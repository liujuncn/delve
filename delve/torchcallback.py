import logging
from typing import List
import torch
from collections import OrderedDict
from torch.nn.modules.activation import ReLU
from torch.nn.modules.conv import Conv2d
from torch.nn.modules.linear import Linear
#from mdp.utils import CovarianceMatrix
from delve.torch_utils import TorchCovarianceMatrix
from delve.writers import CSVWriter, PrintWriter, TensorBoardWriter
from delve.metrics import *

logging.basicConfig(format='%(levelname)s:delve:%(message)s',
                    level=logging.INFO)


class CheckLayerSat(object):
    """Takes PyTorch layers, layer or model as `modules` and writes tensorboardX
    summaries to `logging_dir`. Outputs layer saturation with call to self.saturation().

    Args:
        logging_dir (str)  : destination for summaries
        modules (torch modules or list of modules) : layer-containing object
        log_interval (int) : steps between writing summaries
        stats (list of str): list of stats to collect

            supported stats are:
                lsat       : layer saturation

        sat_method         : How to calculate saturation

            Choice of:

                cumvar99   : Proportion of eigenvalues needed to explain 99% of variance
                simpson_di : Simpson diversity index (weighted sum) of explained variance
                             ratios of eigenvalues
                all        : All available methods are logged

        include_conv       : setting to False includes only linear layers
        conv_method        : how to subsample convolutional layers
        verbose (bool)     : print saturation for every layer during training

    """

    def __init__(
            self,
            savefile: str,
            save_to: str,
            modules,
            log_interval=1,
            min_subsample=128,
            stats: list = ['lsat'],
            layerwise_sat: bool = True,
            reset_covariance: bool = False,
            average_sat: bool = False,
            ignore_layer_names: List[str] = [],
            include_conv: bool = True,
            conv_method: str = 'mean',
            sat_threshold: str = .99,
            verbose=False,
            device='cuda:0'
    ):
        self.verbose = verbose
        self.include_conv = include_conv
        self.conv_method = conv_method
        self.threshold = sat_threshold
        self.layers = self.get_layers_recursive(modules)
        self.min_subsample = min_subsample
        self.reset_covariance = reset_covariance
        self.writer = self._get_writer(save_to, savefile)
        self.interval = log_interval
        self.stats = self._check_stats(stats)
        self.layerwise_sat = layerwise_sat
        self.average_sat = average_sat
        self.ignore_layer_names = ignore_layer_names
        self.logs = {
            'eval-saturation': OrderedDict(),
            'train-saturation': OrderedDict()
        }
        self.global_steps = 0
        self.global_hooks_registered = False
        self.is_notebook = None
        self.device = device
        for name, layer in self.layers.items():
            if isinstance(layer, Conv2d) or isinstance(layer, Linear):
                self._register_hooks(layer=layer,
                                     layer_name=name,
                                     interval=log_interval)

    def __getattr__(self, name):
        if name.startswith('add_') and name != 'add_saturations':
            return getattr(self.writer, name)
        else:
            try:
                # Redirect to writer object
                return self.writer.__getattribute__(name)
            except:
                # Default behaviour
                return self.__getattribute__(name)

    def __repr__(self):
        return self.layers.keys().__repr__()

    def close(self):
        """User endpoint to close writer and progress bars."""
        return self.writer.close()

    def _format_saturation(self, saturation_status):
        raise NotImplementedError

    def _check_stats(self, stats: list):
        if not isinstance(stats, list):
            stats = list(stats)
        supported_stats = [
            'lsat',
        ]
        compatible = [stat in supported_stats for stat in stats]
        incompatible = [i for i, x in enumerate(compatible) if not x]
        assert all(compatible), "Stat {} is not supported".format(
            stats[incompatible[0]])
        return stats

    def _add_conv_layer(self, layer: torch.nn.Module):
        layer.out_features = layer.out_channels
        layer.conv_method = self.conv_method

    def get_layer_from_submodule(self, submodule: torch.nn.Module, layers: dict, name_prefix: str = ''):
            if len(submodule._modules) > 0:
                for idx, (name, subsubmodule) in enumerate(submodule._modules.items()):
                    new_prefix = name if name_prefix == '' else name_prefix+'-'+name
                    self.get_layer_from_submodule(subsubmodule, layers, new_prefix)
                return layers
            else:
                layer_name = name_prefix
                layer_type = layer_name
                if not isinstance(submodule, Conv2d) and not isinstance(submodule, Linear):
                    print(f"Skipping {layer_type}")
                    return layers
                if isinstance(submodule, Conv2d) and self.include_conv:
                    self._add_conv_layer(submodule)
                layers[layer_name] = submodule
                print('added layer {}'.format(layer_name))
                return layers

    def get_layers_recursive(self, modules: Union[list, torch.nn.Module]):
        layers = {}
        if not isinstance(modules, list) and not hasattr(
                modules, 'out_features'):
            # is a model with layers
            # check if submodule
            submodules = modules._modules  # OrderedDict
            layers = self.get_layer_from_submodule(modules, layers, '')
        else:
            for module in modules:
                layers = self.get_layer_from_submodule(module, layers, '')
        return layers

    def _get_writer(self, save_to, savepath):
        """Create a writer to log history to `writer_dir`."""
        if save_to == 'csv':
            writer = CSVWriter(savepath=savepath)
        elif save_to == 'console':
            writer = PrintWriter(savepath=savepath)
        elif save_to == 'tensorboard':
            writer = TensorBoardWriter(savepath=savepath)
        else:
            raise ValueError('Illegal argument for save_to "{}"'.format(save_to))
        return writer

    def _register_hooks(self, layer: torch.nn.Module, layer_name: str,
                        interval):
        if not hasattr(layer, 'eval_layer_history'):
            layer.eval_layer_history = []
        if not hasattr(layer, 'train_layer_history'):
            layer.train_layer_history = []
        if not hasattr(layer, 'layer_svd'):
            layer.layer_svd = None
        if not hasattr(layer, 'forward_iter'):
            layer.forward_iter = 0
        if not hasattr(layer, 'interval'):
            layer.interval = interval
        if not hasattr(layer, 'writer'):
            layer.writer = self.writer
        if not hasattr(layer, 'name'):
            layer.name = layer_name
        self.register_forward_hooks(layer, self.stats)
        return self

    def register_forward_hooks(self, layer: torch.nn.Module, stats: list):
        """Register hook to show `stats` in `layer`."""

        def record_layer_saturation(layer: torch.nn.Module, input, output):
            """Hook to register in `layer` module."""

            # Increment step counter
            layer.forward_iter += 1
            if layer.forward_iter % layer.interval == 0:
                activations_batch = output.data
                training_state = 'train' if layer.training else 'eval'
                layer_history = setattr(layer, f'{training_state}_layer_history', activations_batch)
                eig_vals = None
                if 'lsat' in stats:
                    training_state = 'train' if layer.training else 'eval'

                    if activations_batch.dim() == 4:  # conv layer (B x C x H x W)
                        if self.conv_method == 'median':
                            activations_batch = torch.median(activations_batch, dim=(2, 3))  # channel median
                        elif self.conv_method == 'max':
                            activations_batch = torch.max(activations_batch, dim=(2, 3))  # channel median
                        elif self.conv_method == 'mean':
                            activations_batch = torch.mean(activations_batch, dim=(2, 3))

                    if layer.name in self.logs[f'{training_state}-saturation']:
                        self.logs[f'{training_state}-saturation'][layer.name].update(activations_batch)
                    else:
                        self.logs[f'{training_state}-saturation'][layer.name] = TorchCovarianceMatrix(device=self.device)
                        self.logs[f'{training_state}-saturation'][layer.name]._init_internals(activations_batch)


        layer.register_forward_hook(record_layer_saturation)


    def add_saturations(self):
        """
        Computes saturation and saves all stats
        :return:
        """
        for key in self.logs:
            train_sats = []
            val_sats = []
            if '-saturation' in key:
                for i, layer_name in enumerate(self.logs[key]):
                    if layer_name in self.ignore_layer_names:
                        continue
                    if self.logs[key][layer_name]._cov_mtx is None:
                        raise ValueError('Attempting to compute saturation when covariance is not initialized, try to lower the log interval')
                    sat = compute_saturation(self.logs[key][layer_name]._cov_mtx.cpu().numpy(), thresh=self.threshold)
                    if self.reset_covariance:
                        self.logs[key][layer_name]._cov_mtx = None
                    if self.layerwise_sat:
                        name = key+'_'+layer_name
                        self.writer.add_scalar(name, sat)
                    if 'eval' in key:
                        val_sats.append(sat)
                    elif 'train' in key:
                        train_sats.append(sat)

        if self.average_sat:
            self.writer.add_scalar('average_train_sat', np.mean(train_sats))
            self.writer.add_scalar('average_eval_sat', np.mean(val_sats))

        self.save()


    def save(self):
        self.writer.save()
