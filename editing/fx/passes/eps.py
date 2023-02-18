#
# eps.py
#
# Author(s):
# Georg Rutishauser <georgr@iis.ee.ethz.ch>
# Moritz Scherer <scheremo@iis.ee.ethz.ch>
#
# Copyright (c) 2020-2021 ETH Zurich.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import copy
from typing import Union, Optional
from dataclasses import dataclass

from collections.abc import Iterable

import numpy as np

import torch
from torch import fx, nn

from quantlib.algorithms.pact.pact_ops import *
from quantlib.algorithms.bb.bb_ops import *
from quantlib.algorithms.generic.generic_ops import *

from .pass_base import FxPass, ReplaceSequentialPatternPass
from ..util import gm_modules, module_of_node
import math
import operator

__all__ = ['AnnotateEpsPass',
           'extract_eps']

def eps_conversion_pact_linears(m : nn.Module, eps_in : torch.Tensor):
    ret = m.get_eps_out(eps_in).type_as(eps_in)
    return ret


def eps_conversion_pact_acts(m : nn.Module, eps_in : torch.Tensor):
    return m.get_eps().type_as(eps_in)

def eps_conversion_invalid(m : nn.Module, *eps_in : torch.Tensor, **kw_eps_in : torch.Tensor):
    assert False, f"Module class: {type(m)} does not have a valid epsilon conversion!"

def eps_conversion_pact_gelu(m : nn.Module, eps_in : torch.Tensor):
    return m.get_eps_out(eps_in).type_as(eps_in)

def eps_conversion_pact_matmul(m : nn.Module, *eps_ins):
    return eps_ins[0] * eps_ins[1].type_as(eps_ins[0])

def eps_conversion_matmul(*eps_ins):
    return eps_ins[0] * eps_ins[1].type_as(eps_ins[0])

def eps_conversion_pact_softmax(m : nn.Module, eps_in : torch.Tensor):
    return torch.Tensor((1./(m.n_levels-1.),)).type_as(eps_in)

def eps_conversion_pact_layernorm(m : nn.Module, eps_in : torch.Tensor):
    return m.get_eps_out(eps_in).type_as(eps_in)

def eps_conversion_identity(*eps_ins):
    return eps_ins[0]

def eps_conversion_truediv(m : nn.Module, *eps_ins, **kwargs):
    return m.get_eps_out(eps_ins[0], eps_ins[1]).type_as(eps_ins[0])

def eps_conversion_pact_mean(m : nn.Module, *eps_ins, **kwargs):
    return eps_ins[0]

def eps_conversion_pact_constwrap(m : nn.Module, *eps_ins, **kwargs):
    return m.eps.type_as(eps_ins[0])

def eps_conversion_pact_integeradd(m : nn.Module, *eps_ins, **kwargs):
    return m.act_out.get_eps().type_as(eps_ins[0])

def eps_conversion_embedding(m : nn.Module, eps_in : torch.Tensor):
    return m.adder.act_out.get_eps().type_as(eps_in)

def eps_conversion_PACTWrapModule(m : nn.Module, *eps_in):
    return m.statTracker.get_eps()

def eps_conversion_mul(m : nn.Module, *eps_in):
    return eps_in[0] * eps_in[1].type_as(eps_in[0])

_EPS_CONVERSIONS = {PACTLinear : eps_conversion_pact_linears,
                    PACTConv1d : eps_conversion_pact_linears,
                    PACTConv2d : eps_conversion_pact_linears,
                    PACTAsymmetricAct : eps_conversion_pact_acts,
                    PACTUnsignedAct : eps_conversion_pact_acts,
                    # the hardswish/hardsigmoid activations behave like linears
                    PACTHardswish : eps_conversion_pact_linears,
                    PACTHardsigmoid : eps_conversion_pact_linears,
                    nn.Conv1d : eps_conversion_invalid,
                    nn.Conv2d : eps_conversion_invalid,
                    nn.Conv3d : eps_conversion_invalid,
                    nn.Linear: eps_conversion_invalid,
                    PACTWrapModule : eps_conversion_PACTWrapModule,
                    PACTEmbedding : eps_conversion_embedding,
                    PACTIntegerGELU : eps_conversion_pact_gelu,
                    PACTIntegerSoftmax : eps_conversion_pact_softmax,
                    PACTGELU : eps_conversion_pact_gelu,
                    PACTSoftmax : eps_conversion_pact_softmax,
                    PACTITAMax: eps_conversion_pact_softmax,
                    PACTITAPartialMax: eps_conversion_pact_softmax,
                    PACTIntegerLayerNorm : eps_conversion_pact_layernorm,
                    PACTLayerNorm : eps_conversion_pact_layernorm,
                    PACTIntegerAdd : eps_conversion_pact_integeradd,
                    PACTIntegerMatmul: eps_conversion_pact_matmul,
                    PACTConstWrap: eps_conversion_pact_constwrap,
                    PACTMean: eps_conversion_pact_mean,

                    f'_CALL_FUNCTION_{repr(operator.matmul)}' : eps_conversion_matmul,
                    f'_CALL_FUNCTION_{repr(torch.matmul)}' : eps_conversion_matmul,
                    f'_CALL_FUNCTION_{repr(torch.bmm)}' : eps_conversion_matmul,
                    '_CALL_METHOD_view' : eps_conversion_identity,
                    '_CALL_METHOD_reshape' : eps_conversion_identity,
#                     f'_CALL_FUNCTION_{repr(operator.mul)}' : eps_conversion_mul,
                    PACTDiv : eps_conversion_truediv,
                    Multiply : eps_conversion_mul
}

# modules which "generate" an eps without needing an input eps
_ORIGINAL_EPS_MODULES = (PACTUnsignedAct, PACTAsymmetricAct)

def n_levels_out_invalid(m : nn.Module, in_levels : list, accumulator_levels : int = 2**32):
    assert False, f"Module class: {type(m)} does not have a valid n_levels_out getter!"

def n_levels_out_pact_linears(m : nn.Module, in_levels : list, accumulator_levels : int = 2**32):
    return accumulator_levels

def n_levels_out_truediv(m : nn.Module, in_levels : list, accumulator_levels : int = 2**32):
    return accumulator_levels

def n_levels_out_unchanged(m : nn.Module, in_levels : list, accumulator_levels : int = 2**32):
    return in_levels

def n_levels_out_pact_acts(m : nn.Module, in_levels : list, accumulator_levels : int = 2**32):
    return m.n_levels

def n_levels_out_pact_embedding(m : nn.Module, in_levels : list, accumulator_levels : int = 2**32):
    return m.adder.act_out.n_levels


_N_LEVELS_OUT_PROP = {PACTLinear : n_levels_out_pact_linears,
                      PACTConv1d : n_levels_out_pact_linears,
                      PACTConv2d : n_levels_out_pact_linears,
                      PACTAsymmetricAct : n_levels_out_pact_acts,
                      PACTUnsignedAct : n_levels_out_pact_acts,
                      PACTWrapModule : n_levels_out_pact_acts,
                      PACTEmbedding : n_levels_out_pact_embedding,
                      PACTGELU : n_levels_out_pact_linears,
                      PACTIntegerGELU : n_levels_out_pact_acts,
                      PACTIntegerMatmul : n_levels_out_pact_linears,
                      PACTSoftmax : n_levels_out_pact_acts,
                      PACTIntegerSoftmax : n_levels_out_pact_acts,
                      PACTITAMax: n_levels_out_pact_acts,
                      PACTITAPartialMax: n_levels_out_pact_acts,
                      f'_CALL_FUNCTION_{repr(operator.truediv)}' : n_levels_out_truediv,
                      f'_CALL_FUNCTION_{repr(operator.matmul)}' : n_levels_out_pact_linears,
                      f'_CALL_FUNCTION_{repr(torch.matmul)}' : n_levels_out_pact_linears,
                      f'_CALL_FUNCTION_{repr(torch.bmm)}' : n_levels_out_pact_linears,
                      '_CALL_METHOD_view' : n_levels_out_unchanged,}

always_signed = lambda m, si: True
always_unsigned = lambda m, si: False

def signed_out_pact_wrap(m : nn.Module, si : list):
    return _SIGNED_OUT_PROP[type(m.Module)](m.module, si)
def signed_out_or_in_signed(m : nn.Module, si : list):
    out_signed = False
    for s in si:
        out_signed = out_signed or s
    return out_signed

def has_signed_attr(m : nn.Module, si : list):
    return m.signed

_SIGNED_OUT_PROP = {PACTLinear : always_signed,
                      PACTConv1d : always_signed,
                      PACTConv2d : always_signed,
                      PACTAsymmetricAct : always_signed,
                      PACTUnsignedAct : always_unsigned,
                      PACTWrapModule : signed_out_pact_wrap,
                      PACTGELU : always_signed,
                      PACTIntegerGELU : always_signed,
                      PACTIntegerMatmul : signed_out_or_in_signed,
                      PACTIntegerAdd : signed_out_or_in_signed,
                      PACTSoftmax : always_unsigned,
                      PACTIntegerSoftmax : always_unsigned,
                      PACTITAMax: always_unsigned,
                      PACTITAPartialMax: always_unsigned,
                    BBConv2d : always_signed,
                    BBAct : has_signed_attr,
                    nn.Conv1d : always_signed,
                    nn.Conv2d : always_signed,
                    nn.Linear : always_signed,
                    nn.BatchNorm1d : always_signed,
                    nn.BatchNorm2d : always_signed,
                    nn.MaxPool1d : signed_out_or_in_signed,
                    nn.MaxPool2d : signed_out_or_in_signed,
                    nn.AdaptiveMaxPool1d : signed_out_or_in_signed,
                    nn.AdaptiveMaxPool2d : signed_out_or_in_signed,
                    nn.AvgPool1d : signed_out_or_in_signed,
                    nn.AvgPool2d : signed_out_or_in_signed,
                    nn.AdaptiveAvgPool1d : signed_out_or_in_signed,
                    nn.AdaptiveAvgPool2d : signed_out_or_in_signed,
                      f'_CALL_FUNCTION_{repr(torch.matmul)}' : always_signed,
                      f'_CALL_FUNCTION_{repr(torch.bmm)}' : signed_out_or_in_signed,
                      f'_CALL_FUNCTION_{repr(operator.add)}' : signed_out_or_in_signed,
                    }
@dataclass
class QuantInfo:
    eps_in : torch.Tensor
    eps_out : torch.Tensor
    n_levels_in : list
    n_levels_out : int
    signed_in : list
    signed_out : bool

class AnnotateEpsPass(FxPass):
    def __init__(self, eps_in : Optional[Union[torch.Tensor, float]], n_levels_in : Optional[int] = 256, accumulator_levels : int = 2**32, signed_in : bool = True, prop_eps : bool = True, prop_n_levels : bool = True, prop_sign : bool = True, verbose=False):
        super(AnnotateEpsPass, self).__init__()
        self.verbose = verbose

        if isinstance(eps_in, Iterable):
            try:
                eps_in.__iter__()
                self.eps_in = eps_in
                self.noeps = False
            except:
                self.eps_in = [eps_in]
                self.noeps = False
        elif not isinstance(eps_in, torch.Tensor) and eps_in is not None:
            self.eps_in = [torch.tensor(eps_in).reshape(-1)]
            self.noeps = False
        elif eps_in is None:
            self.eps_in = [torch.tensor(1.0).reshape(1)]
            self.noeps = True
        else:
            self.eps_in = [eps_in.reshape(-1)]
            self.noeps = False

        if n_levels_in is None:
            # providing no n_levels_in is equivalent to providing no eps_in
            self.noeps = True
        self.n_levels_in = n_levels_in

        self.signed_in = signed_in
        self.accumulator_levels = accumulator_levels
        # sometimes we use the AnnotateEpsPass before proper eps propagation is
        # possible (i.e. on unharmonized nets); then eps propagation would fail.
        # In these cases we can disable it.
        self.prop_eps = prop_eps
        self.prop_n_levels = prop_n_levels
        self.prop_sign = prop_sign

    def run_pass(self, gm : fx.GraphModule):
        modules = gm_modules(gm)
        placeHolderIdx = 0
        for node in gm.graph.nodes:
            if node.op == 'placeholder':

                node.meta['quant'] = QuantInfo(eps_in=[self.eps_in[placeHolderIdx]], eps_out=self.eps_in[placeHolderIdx], n_levels_in=self.n_levels_in, n_levels_out=self.n_levels_in, signed_in=[self.signed_in], signed_out=self.signed_in)
                # an equivalent for noeps for signedness is not yet supported...

                placeHolderIdx += 1

                for u in node.users:
                    if self.noeps and self.prop_eps:
                        assert u.op == 'call_module' and isinstance(module_of_node(gm, u), _ORIGINAL_EPS_MODULES), "If no eps is provided to annotate_eps, all users of placeholder nodes must be in _ORIGINAL_EPS_MODULES!"
                    #u.meta['quant'] = QuantInfo(eps_in=torch.tensor(1.0), eps_out=torch.tensor(-1.0))
            else:
                if node.op == 'call_module':
                    m = module_of_node(gm, node)
                    k = type(m)
                else:
                    if node.op == 'get_attr':
                        print(f"[AnnotateEpsPass] get_attr nodes are not currently supported!!")
                        print(f"                    -> Node: {node.name} Key: _{node.op.upper()}_{node.target}")

                    assert node.op != 'get_attr', "get_attr nodes are not currently supported!"

                    k = f'_{node.op.upper()}_{node.target}'
                    m = None

                if self.prop_eps:
                    arg_eps_ins = [i.meta['quant'].eps_out for i in node.args if isinstance(i, fx.Node)]
                    other_args = [i for i in node.args if not isinstance(i, fx.Node)]

                    kwarg_eps_ins = {k : v.meta['quant'].eps_out for k, v in node.kwargs.items() if isinstance(v, fx.Node)}
                    other_kwargs = {k : v for k, v in node.kwargs.items() if not isinstance(v, fx.Node)}
                    conversion_kwargs = copy.copy(other_kwargs)
                    conversion_kwargs.update(other_kwargs)
                    all_eps = arg_eps_ins + [v for v in kwarg_eps_ins.values()]
                    eps_in = [arg_eps_ins, kwarg_eps_ins]
                    if node.op == 'call_module':
                        conversion_args = [m] + arg_eps_ins + other_args
                    else:
                        conversion_args = arg_eps_ins

                    try:
                        eps_out = _EPS_CONVERSIONS[k](*conversion_args, **conversion_kwargs)
                    except KeyError:
                        if (self.verbose): print(f"[AnnotateEpsPass] Key {k} not found in _EPS_CONVERSIONS!")
                        eps_diffs = [torch.abs(e1 - e2) for e1, e2 in zip(all_eps[:-1], all_eps[1:])]
                        if not all(d < 1e-8 for d in eps_diffs):
                            print("[AnnotateEpsPass] Mismatching input epsilons in node with no eps propagation function! Eps propagation will likely be wrong!")
                            print(f"                    -> Node: {node.name} Key: {k}")
                        if (self.verbose): print(f"[AnnotateEpsPass] Using identity epsilon propagation on node with op {node.op}, target {node.target}!")
                        eps_out = all_eps[0]
                else:
                    eps_in = None
                    eps_out = None

                if self.prop_n_levels:
                    node_in_levels = [i.meta['quant'].n_levels_out for i in node.args if isinstance(i, fx.Node)]
                    try:
                        node_out_levels = _N_LEVELS_OUT_PROP[k](m, node_in_levels, self.accumulator_levels)
                    except KeyError:
                        if (self.verbose): print(f"[AnnotateEpsPass] Key {k} not found in _N_LEVELS_OUT_PROP!")
                        if node_in_levels[1:] != node_in_levels[:-1]:
                            print("[AnnotateEpsPass] Mismatching input n_levels in node with no n_levels_out propagation function! n_levels propagation will likely be wrong!")
                            print(f"                    -> Node: {node.name}, Key: : {k}")
                        node_out_levels = node_in_levels[0]
                else:
                    node_in_levels = None
                    node_out_levels = None

                if self.prop_sign:

                    node_in_signed = [i.meta['quant'].signed_out for i in node.args if isinstance(i, fx.Node)]
                    try:
                        node_out_signed = _SIGNED_OUT_PROP[k](m, node_in_signed)
                    except KeyError:
                        if (self.verbose): print(f"[AnnotateEpsPass] Key {k} not found in _SIGNED_OUT_PROP!")
                        if node_in_signed[1:] != node_in_signed[:-1]:
                            print("[AnnotateEpsPass] Mismatching input signedness in node with no signedness propagation function! signedness propagation will likely be wrong!")
                            print(f"                    -> Node: {node.name}, Key: : {k}")
                        node_out_signed = node_in_signed[0]
                else:
                    node_in_signed = None
                    node_out_signed = None
                node.meta['quant'] = QuantInfo(eps_in=eps_in, eps_out=eps_out, n_levels_in=node_in_levels, n_levels_out=node_out_levels, signed_in=node_in_signed, signed_out=node_out_signed)


        return gm


def extract_eps(eps):
    # eps is an annotation produced by annotate_eps which is assumed to
    # contain only 1 value (node has only 1 input). this function extracts the
    # eps tensor from the list[list, dict].
    e_args = eps[0]
    e_kwargs = [e for e in eps[1].values()]
    e = e_args + e_kwargs
    assert len(e) == 1, f"extract_eps got eps containing multiple values: {eps}"
    return e[0]
