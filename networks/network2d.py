#!/usr/bin/python3
import math
import os
import sys
import io
import json
import yaml
import platform
import time
from datetime import datetime
import numpy as np
from copy import deepcopy
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tensorboard import program
import torch.profiler
from torch.profiler import profile, ProfilerActivity
from torch.utils.tensorboard import SummaryWriter
from collections import namedtuple
from collections import OrderedDict
from typing import Callable, Optional
from tqdm import tqdm
import cv2

from pymlutil.torch_util import count_parameters, model_stats, model_weights
from pymlutil.jsonutil import ReadDict, WriteDict, str2bool
from pymlutil.s3 import s3store, Connect
from pymlutil.functions import Exponential
from pymlutil.metrics import DatasetResults
import pymlutil.version as pymlutil_version
from pymlutil.imutil import ImUtil, ImTransform
from pymlutil.version import VersionString

from torchdatasetutil.cocostore import CreateCocoLoaders
from torchdatasetutil.imstore import  CreateImageLoaders
from torchdatasetutil.cityscapesstore import CreateCityscapesLoaders
import torchdatasetutil.version as  torchdatasetutil_version

from ptflops import get_model_complexity_info
from fvcore.nn import FlopCountAnalysis

sys.path.insert(0, os.path.abspath(''))
from networks.cell2d import Cell, PlotSearch, PlotGradients, PlotConvMag, LogTest, WriteModelGraph
from networks.totalloss import TotalLoss, FenceSitterEjectors

class Network2d(nn.Module):
    def __init__(self, 
                 out_channels=1, 
                 source_channels=3, 
                 initial_channels=64, 
                 device=torch.device("cpu"), 
                 unet_depth=5, 
                 max_cell_steps=6, 
                 channel_multiple=2, 
                 batch_norm=False, 
                 cell=Cell, 
                 search_structure=True,
                 residual = False,
                 dropout = False,
                 feature_threshold=0.5,
                 weight_gain = 11.0,
                 convMaskThreshold=0.5,
                 dropout_rate = 0.2,
                 sigmoid_scale = 5.0,
                 k_prune_sigma = 0.33, 
                 search_flops = True,):
        super(Network2d, self).__init__()

        self.unet_depth = unet_depth
        self.out_channels = out_channels
        self.source_channels = source_channels
        self.initial_channels = initial_channels
        self.device = device
        self.cell = cell
        self.max_cell_steps = max_cell_steps
        self.channel_multiple = channel_multiple
        self.batch_norm = batch_norm

        self.cells = torch.nn.ModuleList()
        self.upsample = torch.nn.ModuleList()
        self.final_conv = torch.nn.ModuleList()
        self.search_structure = search_structure
        self.residual = residual
        self.dropout = dropout

        self.feature_threshold=feature_threshold
        self.weight_gain = weight_gain
        self.convMaskThreshold=convMaskThreshold
        self.dropout_rate = dropout_rate
        self.sigmoid_scale = sigmoid_scale
        self.k_prune_sigma = k_prune_sigma
        self.search_flops = search_flops

        encoder_channels = self.initial_channels
        prev_encoder_chanels = self.source_channels
        feedforward_chanels = []
        prev_relaxation = None
        feedforward_relaxation = []

        convolutions=[{'out_channels':encoder_channels, 'kernel_size': 3, 'stride': 1, 'dilation': 1, 'search_structure':True},
                        {'out_channels':encoder_channels, 'kernel_size': 3, 'stride': 1, 'dilation': 1, 'search_structure':True}]
        for i in range(self.unet_depth-1):
            for convolution in convolutions:
                convolution['out_channels'] = encoder_channels

            prevent_collapse = True
                
            prev_relaxation_array = []
            if prev_relaxation is not None:
                prev_relaxation_array.append(prev_relaxation)
            cell = self.cell(prev_encoder_chanels, 
                             prev_relaxation=prev_relaxation_array,
                             batch_norm=self.batch_norm, 
                             device=self.device,
                             convolutions=convolutions,
                             search_structure=self.search_structure,
                             residual=self.residual,
                             dropout=self.dropout,
                             dropout_rate=self.dropout_rate, 
                             sigmoid_scale=self.sigmoid_scale, 
                             feature_threshold=self.feature_threshold, 
                             k_prune_sigma=self.k_prune_sigma,
                             search_flops = self.search_flops,
                             prevent_collapse = prevent_collapse)

            prev_relaxation = cell.cnn[-1].relaxation
            feedforward_relaxation.append(prev_relaxation)
            self.cells.append(cell)

            feedforward_chanels.append(encoder_channels)
            prev_encoder_chanels = encoder_channels
            encoder_channels = int(self.channel_multiple*encoder_channels)

        out_encoder_channels = int(encoder_channels/self.channel_multiple)

        for i in range(self.unet_depth):
            if i == 0:
                feedforward = 0
            else:
                feedforward = feedforward_chanels[-i]

            if i == self.unet_depth-1:
                out_channels = self.out_channels
                final_kernel_size = 1
                final_stride = 1
                conv_transpose = False

            else:
                out_channels =out_encoder_channels
                final_kernel_size = 2
                final_stride = 2
                conv_transpose = True

            if i < self.unet_depth-1:
                search_structure = True
            else:
                 search_structure = False

            prevent_collapse = True

            prev_relaxation_array = [prev_relaxation, feedforward_relaxation[-i]]

            convolutions=[{'out_channels':encoder_channels, 'kernel_size': 3, 'stride': 1, 'dilation': 1, 'search_structure':True},
                          {'out_channels':encoder_channels, 'kernel_size': 3, 'stride': 1, 'dilation': 1, 'search_structure':True},
                          {'out_channels':out_channels, 'kernel_size': final_kernel_size, 'stride': final_stride, 'dilation': 1, 'search_structure':search_structure, 'conv_transpose':conv_transpose}]

            cell = self.cell(prev_encoder_chanels, 
                             feedforward,
                             prev_relaxation=prev_relaxation_array,
                             batch_norm=self.batch_norm,
                             device=self.device,
                             convolutions=convolutions,
                             search_structure=self.search_structure,
                             residual=self.residual,
                             dropout=self.dropout,
                             dropout_rate=self.dropout_rate, 
                             sigmoid_scale=self.sigmoid_scale, 
                             feature_threshold=self.feature_threshold,
                             k_prune_sigma=self.k_prune_sigma,
                             search_flops = self.search_flops,
                             prevent_collapse = prevent_collapse)
            self.cells.append(cell)

            prev_relaxation = cell.cnn[-1].relaxation
            prev_encoder_chanels = out_encoder_channels
            encoder_channels = int(encoder_channels/self.channel_multiple)
            out_encoder_channels = int(encoder_channels/self.channel_multiple)

        self.pool = nn.MaxPool2d(2, 2)

    def ApplyParameters(self, search_structure=None, convMaskThreshold=None, dropout=None, 
                        weight_gain=None, sigmoid_scale=None, feature_threshold=None,
                        k_prune_sigma=None, search_flops=None, batch_norm=None): # Apply a parameter change
        if search_structure is not None:
            self.search_structure = search_structure
        if dropout is not None:
            self.use_dropout = dropout
        if convMaskThreshold is not None:
            self.convMaskThreshold = convMaskThreshold
        if weight_gain is not None:
            self.weight_gain = weight_gain
        if sigmoid_scale is not None:
            self.sigmoid_scale = sigmoid_scale
        if feature_threshold is not None:
            self.feature_threshold = feature_threshold
        if k_prune_sigma is not None:
            self.k_prune_sigma = k_prune_sigma
        if search_flops is not None:
            self.search_flops = search_flops
        if batch_norm is not None:
            self.batch_norm = batch_norm
        for cell in self.cells:
            cell.ApplyParameters(search_structure=search_structure, dropout=dropout, convMaskThreshold=convMaskThreshold,
                                 weight_gain=weight_gain, sigmoid_scale=sigmoid_scale, feature_threshold=feature_threshold,
                                 k_prune_sigma=k_prune_sigma, search_flops=search_flops, batch_norm=batch_norm)

    def forward(self, x):
        feed_forward = []
        enc_len = math.floor(len(self.cells)/2.0)
        iDecode = enc_len

        # Encoder
        for i in range(enc_len):
            x = self.cells[i](x)
            feed_forward.append(x)
            if x is not None:
                x = self.pool(x)

        if (len(self.cells) % 2) != 0:
            x = self.cells[enc_len](x)
            iDecode += 1
        else:
            x = None

        # Decoder
        for i in range(enc_len):
            x = self.cells[iDecode+i](x, feed_forward[-(i+1)])

        return x

    def ApplyStructureConvTranspose2d(self, conv, in_channels=None, out_channels=None):

        if in_channels is not None:
            if len(in_channels) == conv.in_channels:
                conv.weight.data = conv.weight[:, in_channels!=0]
                conv.in_channels = len(in_channels)
            else:
                raise ValueError("len(in_channels)={} must be equal to conv.in_channels={}".format(len(in_channels), conv.in_channels))

        if out_channels is not None:
            if len(out_channels) == conv.out_channels:
                conv.bias.data = conv.bias[out_channels!=0]
                conv.weight.data = conv.weight[out_channels!=0]
                
                print('ConvTranspose2d depth {}/{} = {}'.format(len(in_channels[in_channels!=0]), len(out_channels), len(in_channels[in_channels!=0])/len(out_channels)))
                conv.out_channels = len(out_channels)
            else:
                raise ValueError("len(out_channels)={} must be equal to conv.out_channels={}".format(len(out_channels), conv.out_channels))


    def ApplyStructure(self, msg=None):
        print('ApplyStructure')

        depth = math.ceil(len(self.cells)/2.0)

        print('initial network depth {}/{} = {}'.format(depth, self.unet_depth, depth/self.unet_depth))

        encoder_channel_mask = None
        feedforward_channel_mask = []
        channel_masks = []

        '''_, _, conv_weights = self.ArchitectureWeights()
        newcells = torch.nn.ModuleList()
        for i, conv_weight in enumerate(conv_weights):
            if conv_weight['prune_weight'] < self.feature_threshold:
                print('Prune inactive cell {}'.format(i))
            else:
                newcells.append(self.cells[i])
        self.cells = newcells'''

        # Build up network as if all cells are active
        _, _, conv_weights = self.ArchitectureWeights()
        newcells = torch.nn.ModuleList()
        enc_len = math.floor(len(self.cells)/2.0)
        iDecode = enc_len
        for i in range(enc_len):
            layer_msg = 'Cell {}'.format(i)
            if conv_weights[i]['prune_weight'] < self.feature_threshold:
                prune = True
            else: prune = None
            encoder_channel_mask = self.cells[i].ApplyStructure(encoder_channel_mask, msg=layer_msg, prune=prune)

            feedforward_channel_mask.append(encoder_channel_mask)

        if (len(self.cells) % 2) != 0:
            layer_msg = 'Cell {}'.format(enc_len)
            if conv_weights[enc_len]['prune_weight'] < self.feature_threshold:
                prune = True
            else: prune = None
            encoder_channel_mask = self.cells[enc_len].ApplyStructure(encoder_channel_mask, msg=layer_msg, prune=prune)
            iDecode += 1
        else:
            encoder_channel_mask = torch.zeros_like(encoder_channel_mask, device=self.device) # Only keep feedforward

        for i in range(enc_len):
            iEncDec = i+iDecode
            layer_msg = 'Cell {}'.format(iEncDec)
            if conv_weights[enc_len]['prune_weight'] < self.feature_threshold:
                prune = True
            else: prune = None
            encoder_channel_mask = self.cells[iEncDec].ApplyStructure(encoder_channel_mask, feedforward_channel_mask[-(i+1)], msg=layer_msg, prune=prune)

        return encoder_channel_mask


    def ArchitectureWeights(self):
        architecture_weights = []
        layer_weights = []
        conv_weights = []
        search_structure = []
        model_weights_sum = 0

        for i, l in enumerate(self.cells):
            layer_weight, cnn_weight, conv_weight  = l.ArchitectureWeights()
            architecture_weights.append(layer_weight)
            model_weights_sum += cnn_weight
            conv_weights.append(conv_weight)


        # Reduce cell weight if it may become inactive as a lower cell approaches 0
        depth = math.floor(len(self.cells)/2.0)
        for i in range(depth):
            prune_weight = []
            prune_weight.append(conv_weights[i]['prune_weight'])
            prune_weight.append(conv_weights[-(i+1)]['prune_weight'])
            if i != 0:
                prune_weight.append(conv_weights[i-1]['prune_weight'])
            prune_weight = torch.min(torch.stack(prune_weight))

            conv_weights[i]['prune_weight'] = prune_weight
            conv_weights[-(i+1)]['prune_weight'] = prune_weight
            architecture_weights[i] *= prune_weight
            architecture_weights[-(i+1)] *= prune_weight

        if len(self.cells) > 2 and (len(self.cells) % 2) != 0:
            prune_weight = []
            prune_weight.append(conv_weights[depth]['prune_weight'])
            prune_weight.append(conv_weights[depth-1]['prune_weight'])
            prune_weight = torch.min(torch.stack(prune_weight))

            conv_weights[depth]['prune_weight'] = prune_weight
            architecture_weights[depth] *= prune_weight

        architecture_weights = torch.cat(architecture_weights)
        architecture_weights = architecture_weights.sum_to_size((1))
            
        return architecture_weights, model_weights_sum, conv_weights

def parse_arguments():
    import argparse
    parser = argparse.ArgumentParser(description='Process arguments')

    parser.add_argument('-d', action='store_true',help='Wait for debuggee attach')   
    parser.add_argument('-debug', type=str2bool, default=False, help='Wait for debuggee attach')
    parser.add_argument('-debug_port', type=int, default=3000, help='Debug port')
    parser.add_argument('-debug_address', type=str, default='0.0.0.0', help='Debug port')
    parser.add_argument('-min', action='store_true', help='Minimum run with a few iterations to test execution')
    parser.add_argument('-minimum', type=str2bool, default=False, help='Minimum run with a few iterations to test execution')

    parser.add_argument('-credentails', type=str, default='creds.yaml', help='Credentials file.')
    parser.add_argument('-s3_name', type=str, default='store', help='S3 name in credentials')

    parser.add_argument('-imStatistics', type=str2bool, default=False, help='Record individual image statistics')

    parser.add_argument('-dataset', type=str, default='cityscapes', choices=['coco', 'lit', 'cityscapes'], help='Dataset')
    parser.add_argument('-dataset_path', type=str, default='/data', help='Local dataset path')

    parser.add_argument('-lit_dataset', type=str, default='data/lit/dataset.yaml', help='Image dataset file')
    parser.add_argument('-lit_class_dict', type=str, default='model/crisplit/lit.json', help='Model class definition file.')

    parser.add_argument('-coco_class_dict', type=str, default='model/segmin/coco.json', help='Model class definition file.')

    parser.add_argument('-cityscapes_data', type=str, default='data/cityscapes', help='Image dataset file')
    parser.add_argument('-cityscapes_class_dict', type=str, default='model/cityscapes/cityscapes8.json', help='Model class definition file.')
    parser.add_argument('-sampler', type=bool, default=False, help='Toggle to use WeightedRandomSampler')


    parser.add_argument('-learning_rate', type=float, default=2.0e-4, help='Adam learning rate')
    parser.add_argument('-batch_size', type=int, default=4, help='Training batch size')
    parser.add_argument('-epochs', type=int, default=2, help='Training epochs')
    parser.add_argument('-start_epoch', type=int, default=0, help='Start epoch')

    parser.add_argument('-num_workers', type=int, default=1, help='Data loader workers')
    parser.add_argument('-model_type', type=str,  default='segmentation')
    parser.add_argument('-model_class', type=str,  default='ImgSegmentPrune')
    parser.add_argument('-model_src', type=str,  default='ImgSegmentPrune_cityscapes_20221109_110214_hiocnn_search_structure_04')
    parser.add_argument('-model_dest', type=str, default='ImgSegmentPrune_cityscapes_20221117_110214_hiocnn_plot')
    parser.add_argument('-tb_dest', type=str, default='crisplit_20221018_184000_abacus_tb_01')
    parser.add_argument('-test_sparsity', type=int, default=10, help='test step multiple')
    parser.add_argument('-test_results', type=str, default='cityscapes_test.json')
    parser.add_argument('-cuda', type=str2bool, default=True)
    parser.add_argument('-height', type=int, default=768, help='Batch image height')
    parser.add_argument('-width', type=int, default=512, help='Batch image width')
    parser.add_argument('-unet_depth', type=int, default=5, help='number of encoder/decoder levels to search/minimize')
    parser.add_argument('-max_cell_steps', type=int, default=3, help='maximum number of convolution cells in layer to search/minimize')
    parser.add_argument('-channel_multiple', type=float, default=2, help='maximum number of layers to grow per level')
    parser.add_argument('-k_accuracy', type=float, default=1.0, help='Accuracy weighting factor')
    parser.add_argument('-k_structure', type=float, default=0.5, help='Structure minimization weighting factor')
    parser.add_argument('-k_prune_basis', type=float, default=1.0, help='prune base loss scaling')
    parser.add_argument('-k_prune_exp', type=float, default=50.0, help='prune basis exponential weighting factor')
    parser.add_argument('-k_prune_sigma', type=float, default=1.0, help='prune basis exponential weighting factor')
    parser.add_argument('-target_structure', type=float, default=0.00, help='Structure minimization weighting factor')
    parser.add_argument('-batch_norm', type=str2bool, default=False)
    parser.add_argument('-dropout', type=str2bool, default=False, help='Enable dropout')
    parser.add_argument('-dropout_rate', type=float, default=0.0, help='Dropout probability gain')
    parser.add_argument('-weight_gain', type=float, default=5.0, help='Channel convolution norm tanh weight gain')
    parser.add_argument('-sigmoid_scale', type=float, default=5.0, help='Sigmoid scale domain for convolution channels weights')
    parser.add_argument('-feature_threshold', type=float, default=0.0, help='cell tanh pruning threshold')
    parser.add_argument('-convMaskThreshold', type=float, default=0.5, help='convolution channel sigmoid level to prune convolution channels')
    parser.add_argument('-residual', type=str2bool, default=False, help='Residual convolution functions')
    parser.add_argument('-ejector', type=FenceSitterEjectors, default=FenceSitterEjectors.prune_basis, choices=list(FenceSitterEjectors))
    parser.add_argument('-ejector_start', type=float, default=4, help='Ejector start epoch')
    parser.add_argument('-ejector_full', type=float, default=5, help='Ejector full epoch')
    parser.add_argument('-ejector_max', type=float, default=1.0, help='Ejector max value')
    parser.add_argument('-ejector_exp', type=float, default=3.0, help='Ejector exponent')
    parser.add_argument('-prune', type=str2bool, default=False)
    parser.add_argument('-train', type=str2bool, default=True)
    parser.add_argument('-test', type=str2bool, default=True)
    parser.add_argument('-search_structure', type=str2bool, default=True)
    parser.add_argument('-search_flops', type=str2bool, default=True)
    parser.add_argument('-profile', type=str2bool, default=False)
    parser.add_argument('-time_trial', type=str2bool, default=False)
    parser.add_argument('-onnx', type=str2bool, default=True)
    parser.add_argument('-job', action='store_true',help='Run as job')

    parser.add_argument('-test_name', type=str, default=None, help='Test name for test log' )
    parser.add_argument('-test_path', type=str, default='test/tests.yaml', help='S3 path to test log')
    parser.add_argument('-resultspath', type=str, default='results.yaml')
    parser.add_argument('-prevresultspath', type=str, default=None)
    parser.add_argument('-test_dir', type=str, default='/tb_logs/inference')
    parser.add_argument('-tensorboard_dir', type=str, default='/tb_logs', 
        help='to launch the tensorboard server, in the console, enter: tensorboard --logdir ./tb --bind_all')
    parser.add_argument('-class_weight', type=json.loads, default=None, help='Loss class weight ')
    parser.add_argument('-config', type=str, default='config/build.yaml', help='Configuration file')
    parser.add_argument('-description', type=json.loads, default='{"description":"CRISP segmentation"}', help='Test description')

    args = parser.parse_args()

    if args.d:
        args.debug = args.d
    if args.min:
        args.minimum = args.min

    return args

def ModelSize(args, model, class_dictionary):
    device = torch.device("cpu")

    # Initialize model sizes if needed
    device = torch.device("cpu")
    if args.cuda:
        device = torch.device("cuda")
    input = torch.zeros((1, class_dictionary['input_channels'], args.height, args.width), device=device)

    model(input) # Initialize model sizes if not already
    image_flops, parameters = get_model_complexity_info(deepcopy(model), (class_dictionary['input_channels'], args.height, args.width), as_strings=False,
                                        print_per_layer_stat=False, verbose=False)

    # flops = FlopCountAnalysis(model, input)
    # parameters = count_parameters(model)
    #image_flops = flops.total()

    print('parameters {} flops {}'.format(parameters, image_flops))

    return parameters, image_flops

def load(s3, s3def, args, class_dictionary, loaders, results):

    model = MakeNetwork(class_dictionary, args)
    results['initial_parameters'] , results['initial_flops'] = ModelSize(args, model, class_dictionary)

    print('load initial_parameters = {} initial_flops = {}'.format(results['initial_parameters'], results['initial_flops']))

    if(args.model_src and args.model_src != ''):
        modelObj = s3.GetObject(s3def['sets']['model']['bucket'], '{}/{}/{}.pt'.format(s3def['sets']['model']['prefix'],args.model_class,args.model_src ))

        if modelObj is not None:
            model = torch.load(io.BytesIO(modelObj))

            model_parameters, model_flops = ModelSize(args, model, class_dictionary)
            if not args.model_dest in results['load']:
                results['load'][args.model_dest] = {}
            results['load'][args.model_dest]= {'model_parameters':model_parameters, 'model_flops':model_flops}
            print('load model_parameters = {} model_flops = {}'.format(model_parameters, model_flops))

        else:
            print('Failed to load model_src {}/{}/{}/{}.pt  Exiting'.format(s3def['sets']['model']['bucket'],s3def['sets']['model']['prefix'],args.model_class,args.model_src))
            return model

    return model, results

def save(model, s3, s3def, args, loc=''):
    out_buffer = io.BytesIO()
    model.zero_grad(set_to_none=True)
    #torch.save(model.state_dict(), out_buffer) # To save just state dictionary, need to determine pruned network from state dict
    torch.save(model, out_buffer)
    outname = '{}/{}/{}{}.pt'.format(s3def['sets']['model']['prefix'],args.model_class,args.model_dest,loc)

    print('save {}/{}'.format(s3def['sets']['model']['bucket'], outname))
    succeeded = s3.PutObject(s3def['sets']['model']['bucket'], outname, out_buffer)
    print('s3.PutObject return {}'.format(succeeded))

def save_file(model,outname):
    out_buffer = io.BytesIO()
    model.zero_grad(set_to_none=True)
    torch.save(model, outname)


def MakeNetwork(class_dictionary, args):

    device = torch.device("cpu")
    if args.cuda:
        device = torch.device("cuda")

    network = Network2d(class_dictionary['classes'], source_channels=class_dictionary['input_channels'],
            device=device, 
            unet_depth=args.unet_depth,
            max_cell_steps=args.max_cell_steps, 
            channel_multiple=args.channel_multiple,
            batch_norm=args.batch_norm,
            residual=args.residual,
            dropout=args.dropout,
            feature_threshold = args.feature_threshold,
            weight_gain = args.weight_gain,
            convMaskThreshold = args.convMaskThreshold,
            dropout_rate = args.dropout_rate,
            sigmoid_scale = args.sigmoid_scale,
            k_prune_sigma = args.k_prune_sigma,
            search_flops = args.search_flops)
	
	#specify device for model
    network.to(device)

    return network





def DisplayImgAn(imUtil, image, label, segmentation, trainingset, mean, stdev):
    image = np.squeeze(image)
    label = np.squeeze(label)
    segmentation = np.squeeze(segmentation)
    iman = imUtil.MergeIman(image, label, mean.item(), stdev.item())
    imseg = imUtil.MergeIman(image, segmentation, mean.item(), stdev.item())

    iman = cv2.putText(iman, 'Annotation',(10,25), cv2.FONT_HERSHEY_COMPLEX_SMALL, 1,(255,255,255),1,cv2.LINE_AA)
    imseg = cv2.putText(imseg, 'Segmentation',(10,25), cv2.FONT_HERSHEY_COMPLEX_SMALL, 1,(255,255,255),1,cv2.LINE_AA)
    imanseg = cv2.hconcat([iman, imseg])
    imanseg = cv2.cvtColor(imanseg, cv2.COLOR_BGR2RGB)

    return imanseg

def Train(args, s3, s3def, class_dictionary, model, loaders, device, results, writer, profile=None):

    trainloader = next(filter(lambda d: d.get('set') == 'train', loaders), None)
    testloader = next(filter(lambda d: d.get('set') == 'test' or d.get('set') == 'val', loaders), None)

    if trainloader is None:
        raise ValueError('{} {} failed to load trainloader {}'.format(__file__, __name__, args.dataset)) 
    if testloader is None:
        raise ValueError('{} {} failed to load testloader {}'.format(__file__, __name__, args.dataset))

    # Weight classes for training with heavy class inbalance
    if args.class_weight is not None:
        class_weight = args.class_weight
    elif 'class_weight' in class_dictionary.keys():
        class_weight = class_dictionary['class_weights']
    else:
        class_weight = None

    if class_weight is not None:
        if len(class_weight) == class_dictionary['classes']:
            class_weight = torch.Tensor(class_weight).to(device)
        else:
            print('Parameter error: class weight array length={} must equal number of classes {}.  Exiting'.format(len(args.class_weight), class_dictionary['classes']))
            return

        if args.cuda:
            class_weight = class_weight.cuda()
    else:
        class_weight = None



    # Define a Loss function and optimizer
    target_structure = torch.as_tensor([args.target_structure], dtype=torch.float32, device=device)

    imUtil = ImUtil(trainloader['dataset_dfn'], class_dictionary)

    if args.search_flops:
        total_weights= results['initial_flops'] 
    else:
        total_weights= results['initial_parameters'] 
    loss_fcn = TotalLoss(args.cuda,
                            k_accuracy=args.k_accuracy,
                            k_structure=args.k_structure, 
                            target_structure=target_structure, 
                            class_weight=class_weight, 
                            search_structure=args.search_structure, 
                            k_prune_basis=args.k_prune_basis, 
                            k_prune_exp=args.k_prune_exp,
                            sigmoid_scale=args.sigmoid_scale,
                            ejector=args.ejector,
                            total_weights= total_weights,
                            )
    #optimizer = optim.SGD(model.parameters(), lr=args.learning_rate, momentum=0.9)
    optimizer = optim.Adam(model.parameters(), lr= args.learning_rate)
    plotsearch = PlotSearch()
    plotgrads = PlotGradients()
    plotconvmag = PlotConvMag()

    test_freq = args.test_sparsity*int(math.ceil(trainloader['batches']/testloader['batches']))
    tstart = None
    compression_params = [cv2.IMWRITE_PNG_COMPRESSION, 3]

    results['train'][args.model_dest] = {'loss':[], 'cross_entropy_loss':[], 'architecture_loss':[], 'architecture_reduction':[]}
    # Set up fence sitter ejectors
    ejector_exp = None
    if args.ejector == FenceSitterEjectors.dais or args.ejector == FenceSitterEjectors.dais.value:
        writer.add_scalar('CRISP/sigmoid_scale', args.sigmoid_scale, results['batches'])
        if args.epochs > args.ejector_start and args.ejector_max > args.sigmoid_scale:
            ejector_exp =  Exponential(vx=args.ejector_start, vy=args.sigmoid_scale, px=args.ejector_full, py=args.ejector_max, power=args.ejector_exp)

    elif args.ejector == FenceSitterEjectors.prune_basis or args.ejector == FenceSitterEjectors.prune_basis.value:
        #writer.add_scalar('CRISP/k_prune_basis', args.k_prune_basis, results['batches'])
        if args.epochs > args.ejector_start and args.ejector_max > 0:
            ejector_exp =  Exponential(vx=args.ejector_start, vy=0, px=args.ejector_full, py=args.ejector_max, power=args.ejector_exp)

    write_graph = False
    for epoch in tqdm(range(args.start_epoch, args.epochs), 
                        bar_format='{desc:<8.5}{percentage:3.0f}%|{bar:50}{r_bar}', 
                        desc="Train epochs", disable=args.job):  # loop over the dataset multiple times
        iTest = iter(testloader['dataloader'])

        if ejector_exp is not None:
            if (args.ejector == FenceSitterEjectors.dais or args.ejector == FenceSitterEjectors.dais.value):
                sigmoid_scale = ejector_exp.f(float(epoch)).item()
                model.ApplyParameters(sigmoid_scale=sigmoid_scale, k_prune_sigma=args.k_prune_sigma)
                writer.add_scalar('CRISP/sigmoid_scale', sigmoid_scale, results['batches'])
            elif args.ejector == FenceSitterEjectors.prune_basis or args.ejector == FenceSitterEjectors.prune_basis.value:
                loss_fcn.k_prune_basis = args.k_prune_basis*ejector_exp.f(float(epoch)).item()
            #writer.add_scalar('CRISP/k_prune_basis', loss_fcn.k_prune_basis, results['batches'])

        running_loss = 0.0
        for i, data in tqdm(enumerate(trainloader['dataloader']), 
                            bar_format='{desc:<8.5}{percentage:3.0f}%|{bar:50}{r_bar}', 
                            total=trainloader['batches'], desc="Train batches", disable=args.job):

            try:
                # get the inputs; data is a list of [inputs, labels]
                prevtstart = tstart
                tstart = time.perf_counter()

                inputs, labels, mean, stdev = data

                if args.cuda:
                    inputs = inputs.cuda()
                    labels = labels.cuda()

                # zero the parameter gradients
                optimizer.zero_grad(set_to_none=True)

                #with torch.cuda.amp.autocast():
                outputs = model(inputs)
                tinfer = time.perf_counter()
                loss, cross_entropy_loss, architecture_loss, architecture_reduction, cell_weights, prune_loss, sigmoid_scale = loss_fcn(outputs, labels, model)
                tloss = time.perf_counter()
                loss.backward()
                optimizer.step()
                tend = time.perf_counter()

                dtInfer = tinfer - tstart
                dtLoss = tloss - tinfer
                dtBackprop = tend - tloss
                dtCompute = tend - tstart

                dtCycle = 0
                if prevtstart is not None:
                    dtCycle = tstart - prevtstart

                # print statistics
                running_loss += loss.item()
                training_cross_entropy_loss = cross_entropy_loss
                if writer is not None:
                    writer.add_scalar('loss/train', loss, results['batches'])
                    writer.add_scalar('cross_entropy_loss/train', cross_entropy_loss, results['batches'])
                    writer.add_scalar('time/infer', dtInfer, results['batches'])
                    writer.add_scalar('time/loss', dtLoss, results['batches'])
                    writer.add_scalar('time/backpropegation', dtBackprop, results['batches'])
                    writer.add_scalar('time/compute', dtCompute, results['batches'])
                    writer.add_scalar('time/cycle', dtCycle, results['batches'])
                    writer.add_scalar('CRISP/architecture_loss', architecture_loss, results['batches'])
                    writer.add_scalar('CRISP/prune_loss', prune_loss, results['batches'])
                    writer.add_scalar('CRISP/architecture_reduction', architecture_reduction, results['batches'])
                    #writer.add_scalar('CRISP/sigmoid_scale', sigmoid_scale, results['batches'])

                if i % test_freq == test_freq-1:    # Save image and run test
                    if writer is not None:
                        if cell_weights is not None:
                            imprune_weights = plotsearch.plot(cell_weights)
                            if imprune_weights.size > 0:
                                im_class_weights = cv2.cvtColor(imprune_weights, cv2.COLOR_BGR2RGB)
                                writer.add_image('network/prune_weights', im_class_weights, results['batches'],dataformats='HWC')

                        imgrad = plotgrads.plot(model)
                        if imgrad.size > 0:
                            im_grad_norm = cv2.cvtColor(imgrad, cv2.COLOR_BGR2RGB)
                            writer.add_image('network/gradient_norm', im_grad_norm, results['batches'],dataformats='HWC')

                        convmag = plotconvmag.plot(model)
                        if convmag.size > 0:
                            convmag = cv2.cvtColor(convmag, cv2.COLOR_BGR2RGB)
                            writer.add_image('network/conv_mag', convmag, results['batches'],dataformats='HWC')

                    images = inputs.cpu().permute(0, 2, 3, 1).numpy()
                    labels = np.around(labels.cpu().numpy()).astype('uint8')
                    segmentations = torch.argmax(outputs, 1)
                    segmentations = segmentations.cpu().numpy().astype('uint8')
                    if writer is not None:
                        if not write_graph:
                            writer.add_graph(model, inputs)
                            write_graph = True
                        for j in range(1):
                            imanseg = DisplayImgAn(imUtil, images[j], labels[j], segmentations[j], trainloader['dataloader'], mean[j], stdev[j])      
                            writer.add_image('segmentation/train', imanseg, 0,dataformats='HWC')

                    with torch.no_grad():
                        data = next(iTest)
                        inputs, labels, mean, stdev = data
                        if args.cuda:
                            inputs = inputs.cuda()
                            labels = labels.cuda()

                        #with torch.cuda.amp.autocast():
                        outputs = model(inputs)
                        loss, cross_entropy_loss, architecture_loss, architecture_reduction, cell_weights, prune_loss, sigmoid_scale = loss_fcn(outputs, labels, model)

                    if writer is not None:
                        writer.add_scalar('loss/test', loss, results['batches'])
                        writer.add_scalar('cross_entropy_loss/test', cross_entropy_loss, results['batches'])

                    running_loss /=test_freq
                    msg = '[{:3}/{}, {:6d}/{}]  loss: {:0.5e}|{:0.5e} cross-entropy loss: {:0.5e}|{:0.5e} remaining: {:0.5e} (train|test) compute time: {:0.3f} cycle time: {:0.3f}'.format(
                        epoch + 1, 
                        args.epochs, 
                        i + 1, 
                        trainloader['batches'], 
                        running_loss, loss.item(),
                        training_cross_entropy_loss.item(), 
                        cross_entropy_loss.item(), 
                        architecture_reduction.item(),
                        dtCompute,
                        dtCycle
                    )
                    if args.job is True:
                        print(msg)
                    else:
                        tqdm.write(msg)
                    running_loss = 0.0

                    images = inputs.cpu().permute(0, 2, 3, 1).numpy()
                    labels = np.around(labels.cpu().numpy()).astype('uint8')
                    segmentations = torch.argmax(outputs, 1)
                    segmentations = segmentations.cpu().numpy().astype('uint8')

                    if writer is not None:
                        for j in range(1):
                            imanseg = DisplayImgAn(imUtil,images[j], labels[j], segmentations[j], trainloader['dataloader'], mean[j], stdev[j])      
                            writer.add_image('segmentation/test', imanseg, 0,dataformats='HWC')

                iSave = 1000
                if i % iSave == iSave-1:    # print every iSave mini-batches
                    if cell_weights is not None:
                        img = plotsearch.plot(cell_weights)
                        if img.size > 0:
                            is_success, buffer = cv2.imencode(".png", img, compression_params)
                            img_enc = io.BytesIO(buffer).read()
                            filename = '{}/{}/{}_cw.png'.format(s3def['sets']['model']['prefix'],args.model_class,args.model_dest )
                            s3.PutObject(s3def['sets']['model']['bucket'], filename, img_enc)

                    imgrad = plotgrads.plot(model)
                    if imgrad.size > 0:
                        is_success, buffer = cv2.imencode(".png", imgrad)  
                        img_enc = io.BytesIO(buffer).read()
                        filename = '{}/{}/{}_gn.png'.format(s3def['sets']['model']['prefix'],args.model_class,args.model_dest )
                        s3.PutObject(s3def['sets']['model']['bucket'], filename, img_enc)
                        # Save calls zero_grads so call it after plotgrads.plot

                    save(model, s3, s3def, args)
            
                if profile is not None:
                    profile.step()
            #except:
            except AssertionError:
                print ("Unhandled error in train loop.  Continuing")

            results['batches'] += 1

        if args.minimum and i >= test_freq:
            break

        try:
            if(args.tensorboard_dir is not None and len(args.tensorboard_dir) > 0 and args.tb_dest is not None and len(args.tb_dest) > 0):
                writer_path = '{}/{}'.format(args.tensorboard_dir, args.model_dest)

                if cell_weights is not None:
                    img = plotsearch.plot(cell_weights)
                    if img.size > 0:
                        filename = '{}/{}{:04d}_cw.png'.format(writer_path,args.model_dest, epoch )
                        cv2.imwrite(filename, img)

                        # is_success, buffer = cv2.imencode(".png", img, compression_params)
                        # img_enc = io.BytesIO(buffer).read()
                        #filename = '{}/{}/{}_cw.png'.format(s3def['sets']['model']['prefix'],args.model_class,args.model_dest )
                        #s3.PutObject(s3def['sets']['model']['bucket'], filename, img_enc)


                # Plot gradients before saving which clears the gradients
                imgrad = plotgrads.plot(model)
                if imgrad.size > 0:
                    filename = '{}/{}{:04d}_gn.png'.format(writer_path,args.model_dest, epoch )
                    cv2.imwrite(filename, imgrad)  

                    # is_success, buffer = cv2.imencode(".png", imgrad)  
                    # img_enc = io.BytesIO(buffer).read()
                    # filename = '{}/{}/{}_gn.png'.format(s3def['sets']['model']['prefix'],args.model_class,args.model_dest )
                    # s3.PutObject(s3def['sets']['model']['bucket'], filename, img_enc)

                convmag = plotconvmag.plot(model)
                if convmag.size > 0:
                    filename = '{}/{}{:04d}_cm.png'.format(writer_path,args.model_dest, epoch )
                    cv2.imwrite(filename, convmag)  

                filename = '{}/{}.pt'.format(writer_path,args.model_dest)
                save_file(model, filename)

                if args.minimum:
                    break

                print('{} training complete'.format(args.model_dest))
                results['training'] = {}

            if cross_entropy_loss: results['train'][args.model_dest]['cross_entropy_loss']=cross_entropy_loss.item()
            if architecture_loss: results['train'][args.model_dest]['architecture_loss']=architecture_loss.item()
            if prune_loss: results['train'][args.model_dest]['prune_loss']=prune_loss.item()
            if loss: results['train'][args.model_dest]['loss']=loss.item()
            if architecture_reduction: results['train'][args.model_dest]['architecture_reduction']=architecture_reduction.item()
            save(model, s3, s3def, args)

                # if(args.tensorboard_dir is not None and len(args.tensorboard_dir) > 0 and args.tb_dest is not None and len(args.tb_dest) > 0):
                #     tb_path = '{}/{}/{}'.format(s3def['sets']['model']['prefix'],args.model_class,args.tb_dest )
                #     s3.PutDir(s3def['sets']['test']['bucket'], args.tensorboard_dir, tb_path )
        #except:
        except AssertionError:
            print ("Unhandled error in epoch reporting.  Continuing")

    #save(model, s3, s3def, args)

    return results

def Test(args, s3, s3def, class_dictionary, model, loaders, device, results, writer, profile=None):
    torch.cuda.empty_cache()
    now = datetime.now()
    date_time = now.strftime("%m/%d/%Y, %H:%M:%S")
    test_summary = {'date':date_time}

    testloader = next(filter(lambda d: d.get('set') == 'test' or d.get('set') == 'val', loaders), None)
    if testloader is None:
        raise ValueError('{} {} failed to load testloader {}'.format(__file__, __name__, args.dataset)) 

    if args.test_dir is not None:
        outputdir = '{}/{}/images'.format(args.tensorboard_dir, args.model_dest)
        os.makedirs(outputdir, exist_ok=True)
        tqdm.write('Inference output directory {}'.format(outputdir))

    else:
        outputdir = None

    dsResults = DatasetResults(class_dictionary, args.batch_size, imStatistics=args.imStatistics, imgSave=outputdir)
    tqdm.write(f'saving to {outputdir}')
    dtSum = 0.0
    inferTime = []
    for i, data in tqdm(enumerate(testloader['dataloader']), 
                        total=testloader['batches'], 
                        desc="Test steps", 
                        disable=args.job, 
                        bar_format='{desc:<8.5}{percentage:3.0f}%|{bar:50}{r_bar}'):
        images, labels, mean, stdev = data
        if args.cuda:
            images = images.cuda()

        initial = datetime.now()
        with torch.no_grad():
            outputs = model(images)
            segmentations = torch.argmax(outputs, 1)
        dt = (datetime.now()-initial).total_seconds()
        dtSum += dt
        inferTime.append(dt/args.batch_size)
        #tqdm.write('inferTime = {}'.format(inferTime[-1]))
        writer.add_scalar('test/infer', inferTime[-1], results['batches'])

        if args.time_trial:
            dtSum += dt
        else:
            images = images.cpu().permute(0, 2, 3, 1).numpy()
            labels = np.around(labels.cpu().numpy()).astype('uint8')
            segmentations = segmentations.cpu().numpy().astype('uint8')

            dsResults.infer_results(i, images, labels, segmentations, mean.numpy(), stdev.numpy(), dt)

        if args.minimum and i+1 >= 10:
            break

        if profile is not None:
            profile.step()

    if args.time_trial:
        # test_results = {
        #     'minimum time': float(np.min(inferTime)),
        #     'average time': float(dtSum/testloader['length']),
        #     'num images': testloader['length'],
        # }
        results['test'][args.model_dest] = {
            'minimum time': float(np.min(inferTime)),
            'average time': float(dtSum/testloader['length']),
            'num images': testloader['length'],
        }
    else:
        #test_results = dsResults.Results()
        results['test'][args.model_dest] = dsResults.Results()




    # test_summary['objects'] = dsResults.objTypes
    # test_summary['object store'] =s3def
    # test_summary['results'] = test_results
    # test_summary['config'] = args.__dict__
    # if args.ejector is not None and type(args.ejector) != str:
    #     test_summary['config']['ejector'] = args.ejector.value
    # test_summary['system'] = results['system']
    # test_summary['training_results'] = results

    # # # If there is a way to lock this object between read and write, it would prevent the possability of loosing data
    # test_path = '{}/{}/{}'.format(s3def['sets']['test']['prefix'], args.model_type, args.test_results)
    # training_data = s3.GetDict(s3def['sets']['test']['bucket'], test_path)
    # if training_data is None or type(training_data) is not list:
    #     training_data = []
    # training_data.append(test_summary)
    # s3.PutDict(s3def['sets']['test']['bucket'], test_path, training_data)

    # test_url = s3.GetUrl(s3def['sets']['test']['bucket'], test_path)
    # print("Test results {}".format(test_url))

    # if(args.tensorboard_dir is not None and len(args.tensorboard_dir) > 0 and args.tb_dest is not None and len(args.tb_dest) > 0):
    #     writer_path = '{}/{}/testresults.yaml'.format(args.tensorboard_dir, args.model_dest)
    #     WriteDict(test_summary, writer_path)

    #results['test'] = test_summary['results']
    return results


def Prune(args, s3, s3def, model, class_dictionary, results):
    torch.cuda.empty_cache()
    model.ApplyStructure()

    parameters_after_prune, flops_after_prune = ModelSize(args, model, class_dictionary)

    if(args.tensorboard_dir is not None and len(args.tensorboard_dir) > 0 and args.tb_dest is not None and len(args.tb_dest) > 0):
        writer_path = '{}/{}'.format(args.tensorboard_dir, args.model_dest)
        filename = '{}/{}.pt'.format(writer_path,args.model_dest)
        save_file(model, filename)

    save(model, s3, s3def, args)
    results['prune'][args.model_dest] = {
                        'final parameters':parameters_after_prune, 
                        'initial parameters' : results['initial_parameters'], 
                        'final/intial params': parameters_after_prune/results['initial_parameters'], 
                        'final FLOPS': flops_after_prune, 
                        'initial FLOPS': results['initial_flops'], 
                        'final/intial FLOPS':flops_after_prune/results['initial_flops'] }
    print('{} prune results {}'.format(args.model_dest, yaml.dump(results['prune'], default_flow_style=False)))

    return results

def onnx(model, s3, s3def, args, input_channels):
    torch.cuda.empty_cache()
    import torch.onnx as torch_onnx

    dummy_input = torch.randn(args.batch_size, input_channels, args.height, args.width, device='cuda')
    input_names = ["image"]
    output_names = ["segmentation"]
    oudput_dir = args.tensorboard_dir
    output_filename = '{}/{}.onnx'.format(oudput_dir, args.model_dest)
    dynamic_axes = {input_names[0] : {0 : 'batch_size'},    # variable length axes
                            output_names[0] : {0 : 'batch_size'}}

    os.makedirs(oudput_dir, exist_ok=True)
    torch.onnx.export(model,               # model being run
                dummy_input,                         # model input (or a tuple for multiple inputs)
                output_filename,   # where to save the model (can be a file or file-like object)
                export_params=True,        # store the trained parameter weights inside the model file
                do_constant_folding=True,  # whether to execute constant folding for optimization
                input_names = input_names,   # the model's input names
                output_names = output_names, # the model's output names
                dynamic_axes=dynamic_axes,
                opset_version=11)

    succeeded = s3.PutFile(s3def['sets']['model']['bucket'], output_filename, '{}/{}'.format(s3def['sets']['model']['prefix'],args.model_class) )

def main(args): 
    config = ReadDict(args.config)
    version_str = VersionString(config)
    print('{} version {}'.format(__file__, version_str))

    versions = {
        'platform':str(platform.platform()),
        'python':str(platform.python_version()),
        'numpy': str(np.__version__),
        'torch': str(torch.__version__),
        'OpenCV': str(cv2.__version__),
        'pymlutil': str(pymlutil_version.__version__),
        'network2d':version_str
    }

    results = {
            'batches': 0,
            'initial_parameters': None,
            'initial_flops': None,
            'runs': {},
            'load': {},
            'prune': {},
            'store': {},
            'train': {},
            'test': {},
        }

    results['runs'][args.model_dest] = {
            'arguments': args.__dict__,
            'versions': versions,
        }

    results['runs'][args.model_dest]['arguments']['ejector'] = args.ejector.value # Convert from enum to string
    print('{}'.format(yaml.dump(results, default_flow_style=False) ))

    #torch.autograd.set_detect_anomaly(True)

    s3, _, s3def = Connect(args.credentails, s3_name=args.s3_name)

    results['runs'][args.model_dest]['store'] = s3def

    device = torch.device("cpu")
    if args.cuda:
        device = torch.device("cuda")

    # Load dataset
    class_dictionary = None
    dataset_bucket = s3def['sets']['dataset']['bucket']
    if args.dataset=='coco':
        class_dictionary = s3.GetDict(s3def['sets']['dataset']['bucket'],args.coco_class_dict)
        loaders = CreateCocoLoaders(s3, dataset_bucket, 
            class_dict=args.coco_class_dict, 
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            cuda = args.cuda,
            height = args.height,
            width = args.width,
        )
    elif args.dataset=='lit':
        class_dictionary = s3.GetDict(s3def['sets']['dataset']['bucket'],args.lit_class_dict)
        loaders = CreateImageLoaders(s3, dataset_bucket, 
            dataset_dfn=args.lit_dataset,
            class_dict=args.lit_class_dict, 
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            cuda = args.cuda,
            height = args.height,
            width = args.width,
        )
    elif args.dataset=='cityscapes':
        class_dictionary = s3.GetDict(s3def['sets']['dataset']['bucket'],args.cityscapes_class_dict)

        if 'sample_weights' in class_dictionary.keys() and args.sampler:
            train_sampler_weights = class_dictionary['sample_weights']['weights']
            upsampled_class = class_dictionary['sample_weights']['class']
        else:
            train_sampler_weights = None


        loaders = CreateCityscapesLoaders(s3, s3def, 
            src = args.cityscapes_data,
            dest = args.dataset_path+'/cityscapes',
            class_dictionary = class_dictionary,
            batch_size = args.batch_size, 
            num_workers=args.num_workers,
            height=args.height,
            width=args.width, 
            train_sampler_weights=train_sampler_weights,
            )
    else:
        raise ValueError("Unupported dataset {}".format(args.dataset))

    # Load number of previous batches to continue tensorboard from previous training
    prevresultspath = None
    print('prevresultspath={}'.format(args.prevresultspath))
    if args.prevresultspath and len(args.prevresultspath) > 0:
        prevresults = ReadDict(args.prevresultspath)

        if prevresults is not None:
            results.update(prevresults)
            if 'batches' in prevresults:
                print('found prevresultspath={}'.format(yaml.dump(prevresults, default_flow_style=False)))
                results['batches'] = prevresults['batches']
            if 'initial_parameters' in prevresults:
                results['initial_parameters'] = prevresults['initial_parameters']
                results['initial_flops'] = prevresults['initial_flops']

    segment, results = load(s3, s3def, args, class_dictionary, loaders, results)

    # Prune with loaded parameters than apply current search_structure setting
    segment.ApplyParameters(weight_gain=args.weight_gain, 
                            sigmoid_scale=args.sigmoid_scale,
                            feature_threshold=args.feature_threshold,
                            search_structure=args.search_structure, 
                            convMaskThreshold=args.convMaskThreshold, 
                            k_prune_sigma=args.k_prune_sigma,
                            search_flops=args.search_flops,
                            batch_norm=args.batch_norm)


    # Enable multi-gpu processing
    # if torch.cuda.device_count() > 1:
    #     print("Let's use", torch.cuda.device_count(), "GPUs!")
    #     # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
    #     model = nn.DataParallel(segment)
    # else:
    #     model = segment

    # model_copy = deepcopy(segment)
    # macs, params = get_model_complexity_info(deepcopy(segment), (class_dictionary['input_channels'], args.height, args.width), as_strings=False,
    #                                     print_per_layer_stat=False, verbose=False)
    # results['initial_flops'] = macs
    # print('{:<30}  {:<8}'.format('FLOPS: ', results['initial_flops'] ))
    # print('{:<30}  {:<8}'.format('Number of parameters: ', params))


    tb = None
    writer = None
    writer_path = '{}/{}'.format(args.tensorboard_dir, args.model_dest)

    # Load previous tensorboard for multi-step training
    if(args.tensorboard_dir is not None and len(args.tensorboard_dir) > 0 and args.tb_dest is not None and len(args.tb_dest) > 0):
        tb_path = '{}/{}/{}'.format(s3def['sets']['model']['prefix'],args.model_class,args.tb_dest )
        s3.GetDir(s3def['sets']['test']['bucket'], tb_path, args.tensorboard_dir )
    # Create tensorboard server and tensorboard writer
    if(args.tensorboard_dir is not None and len(args.tensorboard_dir) > 0):
        os.makedirs(args.tensorboard_dir, exist_ok=True)

        print(f"To launch tensorboard server: tensorboard --bind_all --logdir {args.tensorboard_dir}") # https://stackoverflow.com/questions/47425882/tensorboard-logdir-with-s3-path
        writer = SummaryWriter(writer_path)
        WriteModelGraph(args, writer, segment, loaders)

    if args.prune:
        results = Prune(args, s3, s3def, segment, class_dictionary, results)

    if args.train:
        if args.profile:
            with profile(
                    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
                    schedule=torch.profiler.schedule(skip_first=3, wait=1, warmup=1, active=3, repeat=1),
                    on_trace_ready=torch.profiler.tensorboard_trace_handler(writer_path),
                    record_shapes=True, profile_memory=False, with_stack=True, with_flops=False, with_modules=False
            ) as prof:
                results = Train(args, s3, s3def, class_dictionary, segment, loaders, device, results, writer, prof)
        else:
            results = Train(args, s3, s3def, class_dictionary, segment, loaders, device, results, writer)

    if args.test:
        if args.profile:
            with profile(
                    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
                    schedule=torch.profiler.schedule(skip_first=3, wait=1, warmup=1, active=3, repeat=0),
                    on_trace_ready=torch.profiler.tensorboard_trace_handler(writer_path),
                    record_shapes=False, profile_memory=False, with_stack=True, with_flops=False, with_modules=True
            ) as prof:
                results = Test(args, s3, s3def, class_dictionary, segment, loaders, device, results, writer, prof)
        else:
            results = Test(args, s3, s3def, class_dictionary, segment, loaders, device, results, writer)

    if args.onnx:
        onnx(segment, s3, s3def, args, class_dictionary['input_channels'])

    if args.resultspath is not None and len(args.resultspath) > 0:
        WriteDict(results, args.resultspath)

    if(args.tensorboard_dir is not None and len(args.tensorboard_dir) > 0 and args.tb_dest is not None and len(args.tb_dest) > 0):
        results_path = '{}/results.yaml'.format(args.tensorboard_dir)
        WriteDict(results, results_path)

        tb_path = '{}/{}/{}'.format(s3def['sets']['model']['prefix'],args.model_class,args.tb_dest )
        print('Write tensorboard to s3 {}/{}'.format(s3def['sets']['test']['bucket'], args.tensorboard_dir))
        s3.PutDir(s3def['sets']['test']['bucket'], args.tensorboard_dir, tb_path )

    LogTest(args, s3, s3def, results)

    print('Finished {}'.format(args.model_dest ))
    print(yaml.dump(results, default_flow_style=False))
    return 0

if __name__ == '__main__':
    args = parse_arguments()

    if args.debug:
        print("Wait for debugger attach on {}:{}".format(args.debug_address, args.debug_port))
        import debugpy
        ''' 
        https://code.visualstudio.com/docs/python/debugging#_remote-debugging
        Add a "Python: Remote" configuraiton to launch.json:
        "configurations": [
            {
                "name": "Python: Remote",
                "type": "python",
                "request": "attach",
                "port": 3000,
                "host": "localhost",
                "pathMappings": [
                    {
                        "localRoot": "${workspaceFolder}",
                        "remoteRoot": "."
                    }
                ],
                "justMyCode": false
            },
            ...

        Launch application from console with -debug flag
        $ python3 train.py -debug

        Connect to vscode "Python: Remote" configuration
        '''

        debugpy.listen(address=(args.debug_address, args.debug_port)) # Pause the program until a remote debugger is attached
        debugpy.wait_for_client() # Pause the program until a remote debugger is attached
        print("Debugger attached")

    result = main(args)
    sys.exit(result)

