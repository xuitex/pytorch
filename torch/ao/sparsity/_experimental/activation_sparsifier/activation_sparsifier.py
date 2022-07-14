from typing import Dict, Any
import torch
from collections import defaultdict
from torch import nn
import copy
from ...sparsifier.utils import module_to_fqn
import warnings

__all__ = ['ActivationSparsifier']


class ActivationSparsifier:
    r"""
    The Activation sparsifier class aims to sparsify/prune activations in a neural
    network. The idea is to attach the sparsifier to a layer (or layers) and it
    zeroes out the activations based on the mask_fn (or sparsification function)
    input by the user.
    The mask_fn is applied once all the inputs are aggregated and reduced i.e.
    mask = mask_fn(reduce_fn(aggregate_fn(activations)))

    Note::
        The sparsification mask is computed on the input **before it goes through the attached layer**.

    Args:
        model (nn.Module):
            The model whose layers will be sparsified. The layers that needs to be
            sparsified should be added separately using the register_layer() function
        aggregate_fn (Optional, Callable):
            default aggregate_fn that is used if not specified while registering the layer.
            specifies how inputs should be aggregated over time.
            The aggregate_fn should usually take 2 torch tensors and return the aggregated tensor.
            Example
                >>> def add_agg_fn(tensor1, tensor2):  return tensor1 + tensor2
        reduce_fn (Optional, Callable):
            default reduce_fn that is used if not specified while registering the layer.
            reduce_fn will be called on the aggregated tensor i.e. the tensor obtained after
            calling agg_fn() on all inputs.
            Example
                >>> def mean_reduce_fn(agg_tensor):    return agg_tensor.mean(dim=0)
        mask_fn (Optional, Callable):
            default mask_fn that is used to create the sparsification mask using the tensor obtained after
            calling the reduce_fn(). This is used by default if a custom one is passed in the
            register_layer().
            Note that the mask_fn() definition should contain the sparse arguments that is passed in sparse_config
            arguments.
        features (Optional, list):
            default selected features to sparsify.
            If this is non-empty, then the mask_fn will be applied for each feature of the input.
            For example,
                >>> mask = [mask_fn(reduce_fn(aggregated_fn(input[feature])) for feature in features]
        feature_dim (Optional, int):
            default dimension of input features. Again, features along this dim will be chosen
            for sparsification.
        sparse_config (Dict):
            Default configuration for the mask_fn. This config will be passed
            with the mask_fn()

    Example:
        >>> model = SomeModel()
        >>> act_sparsifier = ActivationSparsifier(...)  # init activation sparsifier
        >>> # Initialize aggregate_fn
        >>> def agg_fn(x, y):
        >>>     return x + y
        >>>
        >>> # Initialize reduce_fn
        >>> def reduce_fn(x):
        >>>     return torch.mean(x, dim=0)
        >>>
        >>> # Initialize mask_fn
        >>> def mask_fn(data):
        >>>     return torch.eye(data.shape).to(data.device)
        >>>
        >>>
        >>> act_sparsifier.register_layer(model.some_layer, aggregate_fn=agg_fn, reduce_fn=reduce_fn, mask_fn=mask_fn)
        >>>
        >>> # start training process
        >>>     # epoch starts
        >>>         # model.forward(), compute_loss() and model.backwards()
        >>>     # epoch ends
        >>>     act_sparsifier.step()
        >>> # end training process
        >>> sparsifier.squash_mask()
    """
    def __init__(self, model: nn.Module, aggregate_fn=None, reduce_fn=None, mask_fn=None,
                 features=None, feature_dim=None, **sparse_config):
        self.model = model
        self.defaults: Dict[str, Any] = defaultdict()
        self.defaults['sparse_config'] = sparse_config

        # functions
        self.defaults['aggregate_fn'] = aggregate_fn
        self.defaults['reduce_fn'] = reduce_fn
        self.defaults['mask_fn'] = mask_fn

        # default feature and feature_dim
        self.defaults['features'] = features
        self.defaults['feature_dim'] = feature_dim

        self.data_groups: Dict[str, Dict] = defaultdict(dict)  # contains all relevant info w.r.t each registered layer

        self.state: Dict[str, Any] = defaultdict(dict)  # layer name -> mask

    @staticmethod
    def _safe_rail_checks(args):
        """Makes sure that some of the functions and attributes are not passed incorrectly
        """

        # if features are not None, then feature_dim must not be None
        features, feature_dim = args['features'], args['feature_dim']
        if features is not None:
            assert feature_dim is not None, "need feature dim to select features"

        # all the *_fns should be callable
        fn_keys = ['aggregate_fn', 'reduce_fn', 'mask_fn']
        for key in fn_keys:
            fn = args[key]
            assert callable(fn), 'function should be callable'

    def _aggregate_hook(self, name):
        """Returns hook that computes aggregate of activations passing through.
        """

        # gather some data
        feature_dim = self.data_groups[name]['feature_dim']
        features = self.data_groups[name]['features']
        agg_fn = self.data_groups[name]['aggregate_fn']

        def hook(module, input) -> None:
            input_data = input[0]

            data = self.data_groups[name].get('data')  # aggregated data
            if features is None:
                # no features associated, data should not be a list
                if data is None:
                    data = torch.zeros_like(input_data)
                    self.state[name]['mask'] = torch.ones_like(input_data)
                out_data = agg_fn(data, input_data)
            else:
                # data should be a list [aggregated over each feature only]
                if data is None:
                    out_data = [0 for _ in range(0, len(features))]  # create one incase of 1st forward
                    self.state[name]['mask'] = [0 for _ in range(0, len(features))]
                else:
                    out_data = data  # a list

                # compute aggregate over each feature
                for feature_idx in range(len(features)):
                    # each feature is either a list or scalar, convert it to torch tensor
                    feature_tensor = torch.Tensor([features[feature_idx]], device=input_data.device).long()
                    data_feature = torch.index_select(input_data, feature_dim, feature_tensor)
                    if data is None:
                        curr_data = torch.zeros_like(data_feature)
                        self.state[name]['mask'][feature_idx] = torch.ones_like(data_feature)
                    else:
                        curr_data = data[feature_idx]
                    out_data[feature_idx] = agg_fn(curr_data, data_feature)
            self.data_groups[name]['data'] = out_data
        return hook

    def register_layer(self, layer: nn.Module, aggregate_fn=None, reduce_fn=None,
                       mask_fn=None, features=None, feature_dim=None, **sparse_config):
        r"""
        Registers a layer for sparsification. The layer should be part of self.model.
        Specifically, registers a pre-forward hook to the layer. The hook will apply the aggregate_fn
        and store the aggregated activations that is input over each step.

        Note::
            - There is no need to pass in the name of the layer as it is automatically computed as per
              the fqn convention.

            - All the functions (fn) passed as argument will be called at a dim, feature level.
        """
        name = module_to_fqn(self.model, layer)
        assert name is not None, "layer not found in the model"  # satisfy mypy

        if name in self.data_groups:  # unregister layer if already present
            warnings.warn("layer already attached to the sparsifier, deregistering the layer and registering with new config")
            self.unregister_layer(name=name)

        local_args = copy.deepcopy(self.defaults)
        update_dict = {
            'aggregate_fn': aggregate_fn,
            'reduce_fn': reduce_fn,
            'mask_fn': mask_fn,
            'features': features,
            'feature_dim': feature_dim,
            'layer': layer
        }
        local_args.update((arg, val) for arg, val in update_dict.items() if val is not None)
        local_args['sparse_config'].update(sparse_config)

        self._safe_rail_checks(local_args)

        self.data_groups[name] = local_args
        agg_hook = layer.register_forward_pre_hook(self._aggregate_hook(name=name))

        # attach agg hook
        self.data_groups[name]['hook'] = agg_hook

    def get_mask(self, name: str = None, layer: nn.Module = None):
        """
        Returns mask associated to the layer.

        The mask is
            - a torch tensor is features for that layer is None.
            - a list of torch tensors for each feature, otherwise

        Note::
            The shape of the mask is unknown until model.forward() is applied.
            Hence, if get_mask() is called before model.forward(), an
            error will be raised.
        """
        assert name is not None or layer is not None, "Need at least name or layer obj to retrieve mask"

        if name is None:
            assert layer is not None
            name = module_to_fqn(self.model, layer)
            assert name is not None, "layer not found in the specified model"

        if name not in self.state:
            raise ValueError("Error: layer with the given name not found")

        mask = self.state[name].get('mask', None)

        if mask is None:
            raise ValueError("Error: shape unknown, call layer() routine at least once to infer mask")
        return mask

    def unregister_layer(self, name):
        """Detaches the sparsifier from the layer
        """

        # detach any hooks attached
        self.data_groups[name]['hook'].remove()

        # pop from the state dict
        self.state.pop(name)

        # pop from the data groups
        self.data_groups.pop(name)

    def step(self):
        """
        step() does the following for each registered layer -
            1. apply reduce_fn on the aggregated activations
            2. use mask_fn to compute the sparsification mask
        """
        pass

    def update_mask(self, name, data, **config):
        """Computes and updates mask based on config, feature and dim and data
        """
        pass

    def squash_mask(self, attach_sparsify_hook=True, **kwargs):
        """
        Unregisters aggreagate hook that was applied earlier and registers sparsification hooks if
        attach_sparsify_hook = True.
        """
        pass
