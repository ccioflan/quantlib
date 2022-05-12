import torch
import torch.nn as nn
import torch.fx as fx

import quantlib.editing.graphs as qg
import quantlib.editing.editing as qe


class MNv1Head(nn.Module):

    def __init__(self):
        super(MNv1Head, self).__init__()
        self.eps = qg.nn.EpsTunnel(torch.Tensor([1.0]))
        self.avg = nn.AdaptiveAvgPool2d((1, 1))
        self.lin = nn.Linear(1, 1, bias=True)

    def forward(self, x):
        x = self.eps(x)
        x = self.avg(x)
        x = x.view(x.size(0), 1)
        x = self.lin(x)
        return x


class MNv1HeadApplier(qe.editors.nnmodules.NNModuleApplier):

    def __init__(self, rn18headpattern: qe.editors.nnmodules.GenericNNModulePattern):
        super(MNv1HeadApplier, self).__init__(rn18headpattern)

    def _apply(self, g: fx.GraphModule, ap: qe.editors.nnmodules.NodesMap, id_: str) -> fx.GraphModule:

        name_to_match_node = self.pattern.name_to_match_node(nodes_map=ap)
        node_lin = name_to_match_node['lin']

        name_to_match_module = self.pattern.name_to_match_module(nodes_map=ap, data_gm=g)
        module_eps = name_to_match_module['eps']
        module_lin = name_to_match_module['lin']

        assert module_eps.eps_out.numel() == 1
        assert len(node_lin.all_input_nodes) == 1

        # create the new module
        new_target = id_
        new_module = nn.Linear(in_features=module_lin.in_features, out_features=module_lin.out_features, bias=module_lin.bias is not None)
        new_weight = module_lin.weight.data.detach().clone() * module_eps.eps_out
        new_module.weight.data = new_weight
        if module_lin.bias is not None:
            new_bias = module_lin.bias.data.detach().clone()
            new_module.bias.data = new_bias

        # add the requantised linear operation to the graph...
        g.add_submodule(new_target, new_module)
        linear_input = next(iter(node_lin.all_input_nodes))
        with g.graph.inserting_after(linear_input):
            new_node = g.graph.call_module(new_target, args=(linear_input,))
        node_lin.replace_all_uses_with(new_node)

        module_eps.set_eps_out(torch.ones_like(module_eps.eps_out))

        # ...and delete the old operation
        g.delete_submodule(node_lin.target)
        g.graph.erase_node(node_lin)

        return g


class MNv1HeadRewriter(qe.editors.nnmodules.NNModuleRewriter):

    def __init__(self):
        # create pattern
        rn18headwithcheckers = qe.editors.nnmodules.NNModuleWithCheckers(MNv1Head(), {})
        rn18headpattern = qe.editors.nnmodules.GenericNNModulePattern(qg.fx.quantlib_symbolic_trace, rn18headwithcheckers)
        # create matcher and applier
        finder = qe.editors.nnmodules.GenericGraphMatcher(rn18headpattern)
        applier = MNv1HeadApplier(rn18headpattern)
        # link pattern, matcher, and applier into the rewriter
        super(MNv1HeadRewriter, self).__init__('MNv1HeadRewriter', rn18headpattern, finder, applier)
