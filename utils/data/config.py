import numpy as np
import yaml
import copy

from ..logger import _logger
from .tools import _get_variable_names


def _as_list(x):
    if x is None:
        return None
    elif isinstance(x, (list, tuple)):
        return x
    else:
        return [x]


def _md5(fname):
    '''https://stackoverflow.com/questions/3431825/generating-an-md5-checksum-of-a-file'''
    import hashlib
    hash_md5 = hashlib.md5()
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


class DataConfig(object):
    r"""Data loading configuration.
    """

    def __init__(self, print_info=True, **kwargs):

        opts = {
            'treename': None,
            'selection': None,
            'preprocess': {'method': 'manual', 'data_fraction': 0.1, 'params': None},
            'new_variables': {},
            'inputs': {},
            'labels': {},
            'observers': [],
            'weights': None,
        }
        for k, v in kwargs.items():
            if v is not None:
                if isinstance(opts[k], dict):
                    opts[k].update(v)
                else:
                    opts[k] = v
        # only information in ``self.options'' will be persisted when exporting to YAML
        self.options = opts
        if print_info:
            _logger.debug(opts)

        self.selection = opts['selection']
        self.var_funcs = opts['new_variables']
        # preprocessing config
        self.preprocess = opts['preprocess']
        self._auto_standardization = opts['preprocess']['method'].lower().startswith('auto')
        self._missing_standardization_info = False
        self.preprocess_params = opts['preprocess']['params'] if opts['preprocess']['params'] is not None else {}
        # inputs
        self.input_names = tuple(opts['inputs'].keys())
        self.input_dicts = {k: [] for k in self.input_names}
        self.input_shapes = {}
        for k, o in opts['inputs'].items():
            self.input_shapes[k] = (-1, len(o['vars']), o['length'])
            for v in o['vars']:
                v = _as_list(v)
                self.input_dicts[k].append(v[0])

                if opts['preprocess']['params'] is None:

                    def _get(idx, default):
                        try:
                            return v[idx]
                        except IndexError:
                            return default

                    params = {'length': o['length'], 'center': _get(1, None), 'scale': _get(2, 1), 'min': _get(3, -5), 'max': _get(4, 5), 'pad_value': _get(5, 0)}
                    if v[0] in self.preprocess_params and params != self.preprocess_params[v[0]]:
                        raise RuntimeError('Incompatible info for variable %s, had: \n  %s\nnow got:\n  %s' % (v[0], str(self.preprocess_params[k]), str(params)))
                    if (self._auto_standardization and params['center'] is None) or params['center'] == 'auto':
                        self._missing_standardization_info = True
                    self.preprocess_params[v[0]] = params
        # labels
        self.label_type = opts['labels']['type']
        self.label_value = opts['labels']['value']
        if self.label_type == 'simple':
            assert(isinstance(self.label_value, list))
            self.label_names = ('label',)
            self.var_funcs['label'] = 'np.stack([%s], axis=1).argmax(1)' % (','.join(self.label_value))
        else:
            self.label_names = tuple(self.label_value.keys())
            self.var_funcs.update(self.label_value)
        # weights: TODO
        self.weight_name = None
        if opts['weights'] is not None:
            self.weight_name = 'weight'
            self.use_precomputed_weights = opts['weights']['use_precomputed_weights']
            if self.use_precomputed_weights:
                self.var_funcs[self.weight_name] = '*'.join(opts['weights']['weight_branches'])
            else:
                self.reweight_method = opts['weights']['reweight_method']
                self.reweight_branches = tuple(opts['weights']['reweight_vars'].keys())
                self.reweight_bins = tuple(opts['weights']['reweight_vars'].values())
                self.reweight_classes = tuple(opts['weights']['reweight_classes'])
                self.class_weights = opts['weights'].get('class_weights', None)
                if self.class_weights is None:
                    self.class_weights = np.ones(len(self.reweight_classes))
                self.reweight_hists = opts['weights'].get('reweight_hists', None)
                if self.reweight_hists is not None:
                    for k, v in self.reweight_hists.items():
                        self.reweight_hists[k] = np.array(v, dtype='float32')
        # observers
        self.observer_names = tuple(opts['observers'])

        # remove self mapping from var_funcs
        for k, v in self.var_funcs.items():
            if k == v:
                del self.var_funcs[k]

        if print_info:
            _logger.info('preprocess config: %s', str(self.preprocess))
            _logger.info('selection: %s', str(self.selection))
            _logger.info('var_funcs:\n - %s', '\n - '.join(str(it) for it in self.var_funcs.items()))
            _logger.info('input_names: %s', str(self.input_names))
            _logger.info('input_dicts:\n - %s', '\n - '.join(str(it) for it in self.input_dicts.items()))
            _logger.info('input_shapes:\n - %s', '\n - '.join(str(it) for it in self.input_shapes.items()))
            _logger.info('preprocess_params:\n - %s', '\n - '.join(str(it) for it in self.preprocess_params.items()))
            _logger.info('label_names: %s', str(self.label_names))
            _logger.info('observer_names: %s', str(self.observer_names))

        # parse config
        self.keep_branches = set()
        aux_branches = set()
        # selection
        if self.selection:
            aux_branches.update(_get_variable_names(self.selection))
        # var_funcs
        self.keep_branches.update(self.var_funcs.keys())
        for expr in self.var_funcs.values():
            aux_branches.update(_get_variable_names(expr))
        # inputs
        for names in self.input_dicts.values():
            self.keep_branches.update(names)
        # labels
        self.keep_branches.update(self.label_names)
        # weight
        if self.weight_name:
            self.keep_branches.add(self.weight_name)
            if not self.use_precomputed_weights:
                aux_branches.update(self.reweight_branches)
                aux_branches.update(self.reweight_classes)
        # observers
        self.keep_branches.update(self.observer_names)
        # keep and drop
        self.drop_branches = (aux_branches - self.keep_branches)
        self.load_branches = (aux_branches | self.keep_branches) - set(self.var_funcs.keys()) - {self.weight_name, }
        if print_info:
            _logger.debug('drop_branches:\n  %s', ','.join(self.drop_branches))
            _logger.debug('load_branches:\n  %s', ','.join(self.load_branches))

    def __getattr__(self, name):
        return self.options[name]

    def dump(self, fp):
        with open(fp, 'w') as f:
            yaml.safe_dump(self.options, f, sort_keys=False)

    @classmethod
    def load(cls, fp):
        with open(fp) as f:
            options = yaml.safe_load(f)
        return cls(**options)

    def copy(self):
        return self.__class__(print_info=False, **copy.deepcopy(self.options))

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        return self.copy()

    def export_json(self, fp):
        import json
        j = {'output_names':self.label_value, 'input_names':self.input_names}
        for k, v in self.input_dicts.items():
            j[k] = {'var_names':v, 'var_infos':{}}
            for var_name in v:
                j[k]['var_length'] = self.preprocess_params[var_name]['length']
                center, scale = self.preprocess_params[var_name]['center'], self.preprocess_params[var_name]['scale']
                j[k]['var_infos'][var_name] = {'median':0 if center is None else center, 'norm_factor':scale}
        with open(fp, 'w') as f:
            json.dump(j, f, indent=2)
