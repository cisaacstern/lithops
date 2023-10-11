#
# (C) Copyright Cloudlab URV 2020
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import pika
import os
import sys
import uuid
import json
import logging
import flask
import time
import requests
from functools import partial
from multiprocessing import Process, Value

from lithops.version import __version__
from lithops.utils import setup_lithops_logger, b64str_to_dict, iterchunks
from lithops.worker import function_handler
from lithops.worker.utils import get_runtime_metadata
from lithops.constants import JOBS_PREFIX
from lithops.storage.storage import InternalStorage


logger = logging.getLogger('lithops.worker')

proxy = flask.Flask(__name__)

MASTER_PORT = 8080

JOB_INDEXES = {}


@proxy.route('/getid/<jobkey>/<total_calls>', methods=['GET'])
def get_id(jobkey, total_calls):
    global JOB_INDEXES

    if jobkey not in JOB_INDEXES:
        JOB_INDEXES[jobkey] = 0
    else:
        JOB_INDEXES[jobkey] += 1

    call_id = '-1' if JOB_INDEXES[jobkey] >= int(total_calls) else str(JOB_INDEXES[jobkey])
    remote_host = flask.request.remote_addr
    proxy.logger.info('Sending ID {} to Host {}'.format(call_id, remote_host))

    return call_id


def run_master_server():
    proxy.logger.setLevel(logging.DEBUG)
    proxy.run(debug=True, host='0.0.0.0', port=MASTER_PORT)


def extract_runtime_meta(encoded_payload):
    logger.info(f"Lithops v{__version__} - Generating metadata")

    payload = b64str_to_dict(encoded_payload)

    setup_lithops_logger(payload['log_level'])

    runtime_meta = get_runtime_metadata()

    internal_storage = InternalStorage(payload)
    status_key = '/'.join([JOBS_PREFIX, payload['runtime_name']+'.meta'])
    logger.info(f"Runtime metadata key {status_key}")
    dmpd_response_status = json.dumps(runtime_meta)
    internal_storage.put_data(status_key, dmpd_response_status)


def run_job_k8s(encoded_payload):
    logger.info(f"Lithops v{__version__} - Starting kubernetes execution")

    payload = b64str_to_dict(encoded_payload)
    setup_lithops_logger(payload['log_level'])

    total_calls = payload['total_calls']
    job_key = payload['job_key']
    master_ip = os.environ['MASTER_POD_IP']

    chunksize = payload['chunksize']
    call_ids_ranges = [call_ids_range for call_ids_range in iterchunks(payload['call_ids'], chunksize)]
    data_byte_ranges = payload['data_byte_ranges']

    job_finished = False
    while not job_finished:
        job_index = None

        while job_index is None:
            try:
                url = f'http://{master_ip}:{MASTER_PORT}/getid/{job_key}/{total_calls}'
                res = requests.get(url)
                job_index = int(res.text)
            except Exception:
                time.sleep(0.1)

        if job_index == -1:
            job_finished = True
            continue

        act_id = str(uuid.uuid4()).replace('-', '')[:12]
        os.environ['__LITHOPS_ACTIVATION_ID'] = act_id
        os.environ['__LITHOPS_BACKEND'] = 'k8s'

        logger.info("Activation ID: {} - Job Index: {}".format(act_id, job_index))

        call_ids = call_ids_ranges[job_index]
        dbr = [data_byte_ranges[int(call_id)] for call_id in call_ids]
        payload['call_ids'] = call_ids
        payload['data_byte_ranges'] = dbr

        function_handler(payload)

def run_job_k8s_rabbitmq(payload, job_index, running_jobs):
    logger.info(f"Lithops v{__version__} - Starting kubernetes execution")

    act_id = str(uuid.uuid4()).replace('-', '')[:12]
    os.environ['__LITHOPS_ACTIVATION_ID'] = act_id
    os.environ['__LITHOPS_BACKEND'] = 'k8s_rabbitmq'

    logger.info("Activation ID: {} - Job Index: {}".format(act_id, job_index))
    
    payload['call_ids']  = [payload['call_ids'][job_index]]
    payload['data_byte_ranges'] = [payload['data_byte_ranges'][job_index]]

    start = time.time()
    function_handler(payload)
    stop = time.time()
    logger.info(f"Actual function execution time: {stop - start}")

    running_jobs.value -= 1
    
def calculate_executions(total_cpus, pod_cpus, range, total_functions):
    base = total_functions // total_cpus
    remaining_executions = total_functions % total_cpus

    executions = pod_cpus * base
    
    if range[0] <= remaining_executions <= range[1]:
        remaining_executions = remaining_executions - range[0]
        executions = executions + remaining_executions
        return executions, base
    
    if remaining_executions > range[0]:
        executions = executions + pod_cpus

    return executions, base


if __name__ == '__main__':
    # Checking if is an alternative backend or not
    if "amqp" in sys.argv[1]:
        # checking if it's being called: get_metadata
        if "get_metadata" == sys.argv[1]:
            extract_runtime_meta(sys.argv[2])
            sys.exit()

        rabbitmq_url = sys.argv[1]
        n_processes = int(round(float(sys.argv[2])))

        logger.info(f"Starting Node")
        params = pika.URLParameters(rabbitmq_url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()

        # Range control - begin
        channel.queue_declare(queue='id-assignation')

        # function to assign id ranges
        def receive_range_id(ch, method, properties, body):
            global range_start
            global range_end
            global total_cpus
            msg = json.loads(body)
            range_start = msg["range_start"]
            range_end = msg["range_end"]
            total_cpus = msg["total_cpus"]
            logger.info(f"Range assigned {range_start} - {range_end}")
            logger.info(f"Total cpus {total_cpus}")


        data_reception_id = str(uuid.uuid4())
        channel.queue_declare(queue=data_reception_id)
        channel.basic_consume(queue=data_reception_id, on_message_callback=receive_range_id, auto_ack=True)
        channel.basic_publish(exchange='', routing_key='id-assignation',
                            body=json.dumps({"num_cpus": n_processes, "data_reception_id": data_reception_id, }))
        # Range control - end

        def callback(ch, method, properties, body):
            global range_start
            global range_end
            global total_cpus
            msg = json.loads(body)
            payload = b64str_to_dict(msg["payload"])
            total_calls = payload['total_calls']
            requested_cpus = 0

            logger.info(f"Call from lithops received.")

            if total_calls > range_start:
                if range_end > total_calls - 1:
                    requested_cpus = total_calls - range_start
                else:
                    requested_cpus = range_end - range_start + 1
            else:  # do nothing
                return
            
            pod_cpus = range_end - range_start + 1
            total_executions, bases_executions = calculate_executions(total_cpus, pod_cpus, [range_start, range_end], total_calls)
            
            logger.info(f"Total executions: {total_executions}")
            logger.info(f"Starting {requested_cpus} processes")

            running_jobs = Value('i', 0)  # Shared variable to track completed jobs

            if total_executions == requested_cpus:
                for i in range(requested_cpus):
                    running_jobs.value += 1
                    p = Process(target=run_job_k8s_rabbitmq, args=(payload, range_start + i, running_jobs))
                    p.start()
            else:
                for i in range(pod_cpus):
                    running_jobs.value += 1
                    p = Process(target=run_job_k8s_rabbitmq, args=(payload, range_start + i, running_jobs))
                    p.start()

                total_executions = total_executions - pod_cpus
                
                for bases in range(bases_executions + 2):
                    execution_id = 0
                    while execution_id < pod_cpus and total_executions != 0:
                        if running_jobs.value != pod_cpus:
                            running_jobs.value += 1
                            
                            p = Process(target=run_job_k8s_rabbitmq, args=(payload, (total_cpus * (bases + 1)) + range_start + execution_id, running_jobs))
                            p.start()
                            
                            execution_id += 1
                            total_executions = total_executions - 1
                        else:
                            pass

            logger.info(f"All processes completed")


        # fanout code for receiving payload messages.
        channel.exchange_declare(exchange='lithops', exchange_type='fanout')
        result = channel.queue_declare(queue='', exclusive=True)
        queue_name = result.method.queue
        channel.queue_bind(exchange='lithops', queue=queue_name)
        channel.basic_consume(queue=queue_name, on_message_callback=callback, auto_ack=True)
        try:
            logger.info(f"Listening to rabbitmq...")
            channel.start_consuming()
        finally:
            connection.close()
    else:
        action = sys.argv[1]
        encoded_payload = sys.argv[2]

        switcher = {
            'get_metadata': partial(extract_runtime_meta, encoded_payload),
            'run_job': partial(run_job_k8s, encoded_payload),
            'run_master': run_master_server
        }

        func = switcher.get(action, lambda: "Invalid command")
        func()