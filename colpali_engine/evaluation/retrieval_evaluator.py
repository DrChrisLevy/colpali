import logging
from typing import Any, Dict, List, Optional

import torch
from mteb.evaluation.evaluators import RetrievalEvaluator

from colpali_engine.utils.torch_utils import get_torch_device

logger = logging.getLogger(__name__)


class CustomRetrievalEvaluator:
    """
    Evaluator for retrieval tasks. Supports both single-vector and multi-vector embeddings.
    """

    def __init__(
        self,
        is_multi_vector: bool = False,
        mteb_evaluator_args: Optional[Dict[str, Any]] = None,
        device: str = "auto",
    ):
        self.mteb_evaluator_args = mteb_evaluator_args or {}
        self.is_multi_vector = is_multi_vector
        self.mteb_evaluator = RetrievalEvaluator(**self.mteb_evaluator_args)
        self.device = get_torch_device(device)

    def evaluate(
        self,
        qs: List[torch.Tensor],
        ps: List[torch.Tensor],
    ):
        if self.is_multi_vector:
            scores = self.get_multi_vector_scores(qs, ps)
        else:
            scores = self.get_single_vector_scores(qs, ps)

        assert scores.shape[0] == len(qs), f"Expected {len(qs)} scores, got {scores.shape[0]}"

        arg_score = scores.argmax(dim=1)
        accuracy = (arg_score == torch.arange(scores.shape[0], device=scores.device)).sum().item() / scores.shape[0]

        logger.info(f"Top 1 Accuracy (verif): {accuracy}")

        scores = scores.to(torch.float32).cpu().numpy()
        return scores

    def compute_metrics(
        self,
        relevant_docs: Dict[str, dict[str, int]],
        results: Dict[str, dict[str, float]],
        **kwargs,
    ) -> Dict[str, float]:
        """
        Compute the MTEB retrieval metrics.
        """
        ndcg, _map, recall, precision, naucs = self.mteb_evaluator.evaluate(
            relevant_docs,
            results,
            self.mteb_evaluator.k_values,
            ignore_identical_ids=kwargs.get("ignore_identical_ids", True),
        )

        mrr = self.mteb_evaluator.evaluate_custom(relevant_docs, results, self.mteb_evaluator.k_values, "mrr")

        scores = {
            **{f"ndcg_at_{k.split('@')[1]}": v for (k, v) in ndcg.items()},
            **{f"map_at_{k.split('@')[1]}": v for (k, v) in _map.items()},
            **{f"recall_at_{k.split('@')[1]}": v for (k, v) in recall.items()},
            **{f"precision_at_{k.split('@')[1]}": v for (k, v) in precision.items()},
            **{f"mrr_at_{k.split('@')[1]}": v for (k, v) in mrr[0].items()},
            **{f"naucs_at_{k.split('@')[1]}": v for (k, v) in naucs.items()},
        }
        return scores

    def get_single_vector_scores(
        self,
        qs: List[torch.Tensor],
        ps: List[torch.Tensor],
    ) -> torch.Tensor:
        """
        Compute the dot product score for the given single-vector query and passage embeddings.
        """
        if len(qs) == 0:
            raise ValueError("No querie(s) provided")
        if len(ps) == 0:
            raise ValueError("No passage(s) provided")

        qs_stacked = torch.stack(qs).to(self.device)
        ps_stacked = torch.stack(ps).to(self.device)

        scores = torch.einsum("bd,cd->bc", qs_stacked, ps_stacked)
        return scores

    def get_multi_vector_scores(
        self,
        qs: List[torch.Tensor],
        ps: List[torch.Tensor],
        batch_size=128,
    ) -> torch.Tensor:
        """
        Compute the MaxSim score (ColBERT-like) for the given multi-vector query and passage embeddings.
        """
        if len(qs) == 0:
            raise ValueError("No querie(s) provided")
        if len(ps) == 0:
            raise ValueError("No passage(s) provided")

        scores_list: List[torch.Tensor] = []

        for i in range(0, len(qs), batch_size):
            scores_batch = []
            qs_batch = torch.nn.utils.rnn.pad_sequence(qs[i : i + batch_size], batch_first=True, padding_value=0).to(
                self.device
            )
            for j in range(0, len(ps), batch_size):
                ps_batch = torch.nn.utils.rnn.pad_sequence(
                    ps[j : j + batch_size], batch_first=True, padding_value=0
                ).to(self.device)
                scores_batch.append(torch.einsum("bnd,csd->bcns", qs_batch, ps_batch).max(dim=3)[0].sum(dim=2))
            scores_batch = torch.cat(scores_batch, dim=1).cpu()
            scores_list.append(scores_batch)

        scores = torch.cat(scores_list, dim=0)
        return scores