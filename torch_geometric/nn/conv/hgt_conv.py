import math
from collections import defaultdict
from typing import Dict, List, Optional, Union

import torch
import torch.nn.functional as F
from torch import Tensor
from torch.nn import Parameter

from torch_geometric.data.hetero_data import (
    combine_edge_slices,
    make_node_slices,
    offset_edge_idx,
)
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.nn.dense import Linear
from torch_geometric.nn.inits import glorot, ones, reset
from torch_geometric.nn.parameter_dict import ParameterDict
from torch_geometric.typing import (
    EdgeType,
    Metadata,
    NodeType,
    SparseTensor,
    grouped_matmul_avail,
    pyg_lib,
)
from torch_geometric.utils import softmax


def group(xs: List[Tensor], aggr: Optional[str]) -> Optional[Tensor]:
    if len(xs) == 0:
        return None
    elif aggr is None:
        return torch.stack(xs, dim=1)
    elif len(xs) == 1:
        return xs[0]
    elif aggr == "cat":
        return torch.cat(xs, dim=-1)
    else:
        out = torch.stack(xs, dim=0)
        out = getattr(torch, aggr)(out, dim=0)
        out = out[0] if isinstance(out, tuple) else out
        return out


class HGTConv(MessagePassing):
    r"""The Heterogeneous Graph Transformer (HGT) operator from the
    `"Heterogeneous Graph Transformer" <https://arxiv.org/abs/2003.01332>`_
    paper.

    .. note::

        For an example of using HGT, see `examples/hetero/hgt_dblp.py
        <https://github.com/pyg-team/pytorch_geometric/blob/master/examples/
        hetero/hgt_dblp.py>`_.

    Args:
        in_channels (int or Dict[str, int]): Size of each input sample of every
            node type, or :obj:`-1` to derive the size from the first input(s)
            to the forward method.
        out_channels (int): Size of each output sample.
        metadata (Tuple[List[str], List[Tuple[str, str, str]]]): The metadata
            of the heterogeneous graph, *i.e.* its node and edge types given
            by a list of strings and a list of string triplets, respectively.
            See :meth:`torch_geometric.data.HeteroData.metadata` for more
            information.
        heads (int, optional): Number of multi-head-attentions.
            (default: :obj:`1`)
        group (str, optional): The aggregation scheme to use for grouping node
            embeddings generated by different relations
            (:obj:`"sum"`, :obj:`"mean"`, :obj:`"min"`, :obj:`"max"`).
            (default: :obj:`"sum"`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """
    def __init__(
        self,
        in_channels: Union[int, Dict[str, int]],
        out_channels: int,
        metadata: Metadata,
        heads: int = 1,
        group: str = "sum",
        **kwargs,
    ):
        super().__init__(aggr='add', node_dim=0, **kwargs)

        if out_channels % heads != 0:
            raise ValueError(f"'out_channels' (got {out_channels}) must be "
                             f"divisible by the number of heads (got {heads})")

        if not isinstance(in_channels, dict):
            in_channels = {node_type: in_channels for node_type in metadata[0]}
        self.use_gmm = grouped_matmul_avail()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.heads = heads
        self.group = group

        self.node_types = metadata[0]
        self.edge_types = metadata[1]
        self.src_types = [edge_type[0] for edge_type in self.edge_types]
        if self.use_gmm: # pragma: no cover
            # grouped gemm allows us not to have to pad
            from torch_geometric.nn.dense import HeteroDictLinear
            self.k_lin = HeteroDictLinear(self.in_channels, self.out_channels,
                                          **kwargs)
            self.q_lin = HeteroDictLinear(self.in_channels, self.out_channels,
                                          **kwargs)
            self.v_lin = HeteroDictLinear(self.in_channels, self.out_channels,
                                          **kwargs)
            self.a_lin = HeteroDictLinear(self.out_channels, self.out_channels,
                                          self.node_types, **kwargs)
        else:
            from torch_geometric.nn.to_hetero_module import ToHeteroLinear
            self.max_channels = max(self.in_channels.values())
            self.k_lin = ToHeteroLinear(
                Linear(self.max_channels, self.out_channels, **kwargs),
                self.node_types)
            self.q_lin = ToHeteroLinear(
                Linear(self.max_channels, self.out_channels, **kwargs),
                self.node_types)
            self.v_lin = ToHeteroLinear(
                Linear(self.max_channels, self.out_channels, **kwargs),
                self.node_types)
            self.a_lin = ToHeteroLinear(
                Linear(self.out_channels, self.out_channels, **kwargs),
                self.node_types)
        self.skip = ParameterDict({
            node_type: Parameter(torch.Tensor(1))
            for node_type in self.node_types
        })

        self.a_rel = ParameterDict()
        self.m_rel = ParameterDict()
        self.p_rel = ParameterDict()
        dim = out_channels // heads
        for edge_type in metadata[1]:
            edge_type = '__'.join(edge_type)
            self.a_rel[edge_type] = Parameter(torch.Tensor(heads, dim, dim))
            self.m_rel[edge_type] = Parameter(torch.Tensor(heads, dim, dim))
            self.p_rel[edge_type] = Parameter(torch.Tensor(heads))

        self.reset_parameters()
        major_vers, minor_vers = str(torch.__version__).split('.')[:2]

    def reset_parameters(self):
        super().reset_parameters()
        reset(self.k_lin)
        reset(self.q_lin)
        reset(self.v_lin)
        reset(self.a_lin)
        ones(self.skip)
        ones(self.p_rel)
        glorot(self.a_rel)
        glorot(self.m_rel)

    def forward(
        self,
        x_dict: Dict[NodeType, Tensor],
        edge_index_dict: Union[Dict[EdgeType, Tensor],
                               Dict[EdgeType, SparseTensor]]  # Support both.
    ) -> Dict[NodeType, Optional[Tensor]]:
        r"""
        Args:
            x_dict (Dict[str, Tensor]): A dictionary holding input node
                features  for each individual node type.
            edge_index_dict (Dict[str, Union[Tensor, SparseTensor]]): A
                dictionary holding graph connectivity information for each
                individual edge type, either as a :obj:`torch.LongTensor` of
                shape :obj:`[2, num_edges]` or a
                :obj:`torch_sparse.SparseTensor`.

        :rtype: :obj:`Dict[str, Optional[Tensor]]` - The output node embeddings
            for each node type.
            In case a node type does not receive any message, its output will
            be set to :obj:`None`.
        """
        H, D = self.heads, self.out_channels // self.heads

        k_dict, q_dict, v_dict = {}, {}, {}
        out_dict = defaultdict(list)

        # parallelize over node-types
        # compute K, Q, V over node-types
        k_dict = {
            node_type: k_j.view(-1, H, D)
            for node_type, k_j in self.k_lin(x_dict).items()
        }
        q_dict = {
            node_type: q_j.view(-1, H, D)
            for node_type, q_j in self.q_lin(x_dict).items()
        }
        v_dict = {
            node_type: v_j.view(-1, H, D)
            for node_type, v_j in self.v_lin(x_dict).items()
        }

        # parallelize over edge-types
        a_rels = [
            self.a_rel['__'.join(edge_type)] for edge_type in self.edge_types
        ]
        m_rels = [
            self.m_rel['__'.join(edge_type)] for edge_type in self.edge_types
        ]
        if self.use_gmm: # pragma: no cover
            k_ins = [
                k_dict[src_type].transpose(0, 1) for src_type in self.src_types
            ]
            v_ins = [
                v_dict[src_type].transpose(0, 1) for src_type in self.src_types
            ]
            k_outs = [
                k_o_i.transpose(1, 0)
                for k_o_i in pyg_lib.ops.grouped_matmul(k_ins, a_rels)
            ]
            v_outs = [
                v_o_i.transpose(1, 0)
                for v_o_i in pyg_lib.ops.grouped_matmul(v_ins, m_rels)
            ]
            node_slices = make_node_slices({
                n_type: k_outs[i].shape[0]
                for i, n_type in enumerate(self.node_types)
            })
            k_out = torch.cat(k_outs)
            v_out = torch.cat(v_outs)
        else:
            k_ins = []
            v_ins = []
            count = 0
            trans_ptr = [count]
            for src_type in self.src_types:
                k_src = k_dict[src_type]
                v_src = v_dict[src_type]
                k_ins.append(k_src.view(-1, D))
                v_ins.append(v_src.view(-1, D))
                for i in range(H):
                    count += k_src.size(0)
                    trans_ptr.append(count)
            trans_ptr = torch.tensor(trans_ptr)
            a_rel, m_rel = torch.cat(a_rels), torch.cat(m_rels)
            k_out = pyg_lib.ops.segment_matmul(torch.cat(k_ins), trans_ptr,
                                               a_rel).view(-1, H, D)

            v_out = pyg_lib.ops.segment_matmul(torch.cat(v_ins), trans_ptr,
                                               m_rel).view(-1, H, D)
            node_slices = make_node_slices({
                n_type: k_out[trans_ptr[i]:trans_ptr[i + 1]].shape[0]
                for i, n_type in enumerate(self.node_types)
            })

        # combine edge_index dict into single tensor
        q_list = []
        p_rels = []
        edge_index_list = []
        for e_type in self.edge_types:
            indices = edge_index_dict[e_type]
            # (TODO) Add support for SparseTensor w/o converting?
            convert = isinstance(indices, SparseTensor)
            if convert:
                # convert to COO
                dst, src, _ = indices.coo()
                indices = torch.cat((src.view(1, -1), dst.view(1, -1)))
            edge_index_list.append(
                offset_edge_idx(node_slices, e_type, indices))
            q_list.append(q_dict[e_type[-1]])
            p_rels.append(self.p_rel['__'.join(e_type)].view(-1, 1))

        q = torch.cat(q_list)
        p = group(p_rels, self.group).view(-1)
        e_idx = combine_edge_slices(edge_index_list)
        if convert:
            # convert back to CSR
            e_idx = SparseTensor(row=e_idx[0], col=e_idx[1],
                                 sparse_sizes=(k_out.size(0), k_out.size(0)))

        # propagate
        out = self.propagate(e_idx, k=k_out, q=q, v=v_out, rel=p, size=None)
        k_ptr = 0
        for node_type, k in k_dict.items():
            out_dict[node_type] = out[k_ptr:k_ptr + k.size(0)]
            k_ptr += k.size(0)

        # parralelize over node-types
        a_dict = self.a_lin({
            node_type: F.gelu(out)
            for node_type, out in out_dict.items() if out is not None
        })

        # Iterate over node-types:
        for node_type, out in out_dict.items():
            if out is None:
                out_dict[node_type] = None
                continue
            else:
                out = a_dict[node_type]

            if out.size(-1) == x_dict[node_type].size(-1):
                alpha = self.skip[node_type].sigmoid()
                out = alpha * out + (1 - alpha) * x_dict[node_type]
            out_dict[node_type] = out

        return out_dict

    def message(self, k_j: Tensor, q_i: Tensor, v_j: Tensor, rel: Tensor,
                index: Tensor, ptr: Optional[Tensor],
                size_i: Optional[int]) -> Tensor:
        alpha = (q_i * k_j).sum(dim=-1) * rel
        alpha = alpha / math.sqrt(q_i.size(-1))
        alpha = softmax(alpha, index, ptr, size_i)
        out = v_j * alpha.view(-1, self.heads, 1)
        return out.view(-1, self.out_channels)

    def __repr__(self) -> str:
        return (f'{self.__class__.__name__}(-1, {self.out_channels}, '
                f'heads={self.heads})')
