#!/usr/bin/python3

import sys
import os
import requests
import random
import json
import yaml
from pathlib import Path
from datetime import datetime, timedelta


from pymlutil.s3 import s3store, Connect
from pymlutil.jsonutil import ReadDict, Dict2Json

# paraemters is a dictionary of parameters to set
def set_parameters(workflow, new_parameters):
    if 'arguments' in workflow['workflow']['spec']:
        if 'parameters' in workflow['workflow']['spec']['arguments']:
            parameters = workflow['workflow']['spec']['arguments']['parameters']
            for parameter in parameters:
                for key, value in new_parameters.items():
                    if key == parameter['name']:
                        if type(value) is dict:
                            parameter['value'] = json.dumps(value)
                        else:
                            parameter['value'] = value

def get_parameter(workflow, name):
    if 'arguments' in workflow['workflow']['spec']:
        if 'parameters' in workflow['workflow']['spec']['arguments']:
            for parameter in workflow['workflow']['spec']['arguments']['parameters']:
                if name == parameter['name']:
                    return parameter
    return None

def run(workflow, argocreds):
    session = requests.session()

    workflowstr = '{}://{}/api/v1/workflows/{}'.format(
        'https' if argocreds['tls'] else 'http',
        argocreds['address'],
        argocreds['namespace'])

    tasks_resp = session.post(workflowstr, json=workflow, verify = False)
    print('url: {} \nstatus_code: {} \nresponse: {}'.format(tasks_resp.url, tasks_resp.status_code, tasks_resp.text))
    return tasks_resp 


def parse_arguments():
    import argparse
    parser = argparse.ArgumentParser(description='Process arguments')

    parser.add_argument('--debug', '-d', action='store_true',help='Wait for debuggee attach')   
    parser.add_argument('-debug_port', type=int, default=3000, help='Debug port')
    parser.add_argument('-debug_address', type=str, default='0.0.0.0', help='Debug port')
    parser.add_argument('-test', action='store_true', help='Run unit tests')
    parser.add_argument('-testfile', type=str, default='tests.yaml', help='Test')

    parser.add_argument('-config', type=str, default='config/build.yaml', help='Configuration file')
    parser.add_argument('-image', type=str, default='crisptrain', help='Workflow image name')

    parser.add_argument('-credentails', type=str, default='creds.yaml', help='Credentials file.')
    parser.add_argument('-objectserver', type=str, default='store', help='Object server name.')
    parser.add_argument('--name', '-n', type=str, default=None, help='Test name.  Default is model_class_dataset_timestamp from workflow')
    parser.add_argument('--server', '-s', type=str, default='abacus', help='Argo Server.')
    parser.add_argument('--run', '-r', type=str, default='workflow/litcrisp.yaml', help='Run workflow')
    parser.add_argument('--name_prefix_param', type=str, default='model_class', help='Workflow parameter providing the name prefix')
    parser.add_argument('--set_prefix_param', type=str, default='dataset', help='Workflow parameter providing the dataset prefix')

    #param_str = '{"description":{"author": "sherlg", "description":"Test workflow logging 6"}}'
    param_str = '{"description":{"author": "sherlg", "description":"Test workflow logging 6"}, "target_structure": 0.0, "batch_size": 2, "debug": "true"}'
    help_str = "Parameters parsed by set_parameters  e.g.: -p '{}'".format(param_str)
    parser.add_argument('--params', '-p', type=json.loads, default=None, help=help_str)

    args = parser.parse_args()
    return args

def ImageName(image_names, image):
    for image_entry in image_names:
        if image == image_entry['name']:
            return image_entry['image_name']
    return None

def LogTest(args, s3, s3def, test_time, workflow, argocreds, tasks_resp):

    if tasks_resp.ok == True:
        resp_dict = json.loads(tasks_resp.text)

        description = ''
        imgage = ''
        test_name = ''
        model_class = ''
        dataset = ''
        test_path = ''
        parameters = workflow['workflow']['spec']['arguments']['parameters']
        for parameter in parameters:
            if 'description' == parameter['name']:
                description = parameter['value']
            elif 'output_name' == parameter['name']:
                test_name = parameter['value']
            elif args.name_prefix_param == parameter['name']:
                model_class = parameter['value']
            elif args.set_prefix_param == parameter['name']:
                dataset = parameter['value']
            elif 'train_image' == parameter['name']:
                imgage = parameter['value']
            elif 'test_path' == parameter['name']:
                test_path = parameter['value']

        testworkflow = Path(args.run)
        workflow_path = '{}/workflows/{}_{}{}'.format(s3def['sets']['test']['prefix'],  test_name, testworkflow.stem, testworkflow.suffix)
        s3.PutDict(s3def['sets']['test']['bucket'], workflow_path, workflow)

        test_summary = {
            'name': test_name,
            'when': test_time.strftime("%c"),
            'server': argocreds['name'],
            'image': imgage,
            'workflow': workflow_path,
            'model_class': model_class,
            'dataset': dataset,
            'job': resp_dict['metadata']['name'],
            'tensorboard': '{}_tb'.format(test_name),
            'description': description,
        }

        test_data = s3.GetDict(s3def['sets']['test']['bucket'], test_path)
        if test_data is None or type(test_data) is not list:
            test_data = []
        test_data.append(test_summary)
        s3.PutDict(s3def['sets']['test']['bucket'], test_path, test_data)
    else:
        test_summary = None

    return test_summary

def main(args):

    s3, creds, s3def = Connect(args.credentails, s3_name=args.objectserver)
    if not s3:
        print("Failed to connect to s3 {} name {} ".format(args.credentails, args.objectserver))
        return -1

    argocreds = None
    if 'argo' in creds:
        if args.server is not None:
            argocreds = next(filter(lambda d: d.get('name') == args.server, creds['argo']), None)
        else:
            #argocreds = random.choice(creds['argo'])
            argocreds = creds['argo'][0]

    if not argocreds:
        print("Failed to find argo credentials for {}".format(args.server))
        return -1

    workflow = ReadDict(args.run)
    if not workflow:
        print('Failed to read {}'.format(args.run))
        return -1


    config = ReadDict(args.config)
    test_time = datetime.now()

    if args.name is None:
        prefix = ''
        name_prefix = get_parameter(workflow, args.name_prefix_param)
        if name_prefix:
            prefix += name_prefix['value'] + '_'

        set_prefix = get_parameter(workflow, args.set_prefix_param)
        if set_prefix:
            prefix += set_prefix['value'] + '_'

        args.name = '{}{}_{}'.format(prefix, test_time.strftime("%Y%m%d_%H%M%S"),args.server)

    imageName = ImageName(config['image_names'], args.image)

    test_path = '{}/{}'.format(s3def['sets']['test']['prefix'],  args.testfile)
    set_parameters(workflow, {'output_name': args.name, 'train_image': imageName, 'test_path': test_path})

    if args.params is not None and len(args.params) > 0:
        set_parameters(workflow, args.params)
    tasks_resp = run(workflow, argocreds)


    test_summary = LogTest(args, s3, s3def, test_time, workflow, argocreds, tasks_resp)

    print('{}'.format(yaml.dump(test_summary, default_flow_style=False) ))

    return 0 if tasks_resp.ok == True else -1 


if __name__ == '__main__':
    args = parse_arguments()

    if args.debug:
        print("Wait for debugger attach on {}:{}".format(args.debug_address, args.debug_port))
        import debugpy
        debugpy.listen(address=(args.debug_address, args.debug_port))
        # Pause the program until a remote debugger is attached

        debugpy.wait_for_client()
        print("Debugger attached")

    result = main(args)
    sys.exit(result)