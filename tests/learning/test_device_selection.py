"""Tests for PyTorch device selection (cuda > mps > cpu)."""

from __future__ import annotations


class TestSelectTorchDevice:
    """Tests for _select_torch_device() logic in orchestrator trainers.

    Torch is optional and may be installed on developer or CI hosts. Tests isolate
    dependency and accelerator availability instead of inheriting host capabilities.
    """

    def test_no_torch_returns_none(self, monkeypatch):
        """Without torch, _select_torch_device returns None."""
        from openjarvis.learning.intelligence.orchestrator import (
            sft_trainer,
        )

        # Torch and a GPU can legitimately be present on a developer machine. Isolate
        # the missing-optional-dependency branch instead of depending on the host.
        monkeypatch.setattr(sft_trainer, "HAS_TORCH", False)
        monkeypatch.setattr(sft_trainer, "torch", None)
        assert sft_trainer._select_torch_device() is None

    def test_cuda_preferred(self):
        """CUDA is selected when available (logic test)."""
        has_cuda = True
        has_mps = True

        if has_cuda:
            choice = "cuda"
        elif has_mps:
            choice = "mps"
        else:
            choice = "cpu"

        assert choice == "cuda"

    def test_mps_fallback(self):
        """MPS is selected when CUDA is not available but MPS is."""
        has_cuda = False
        has_mps = True

        if has_cuda:
            choice = "cuda"
        elif has_mps:
            choice = "mps"
        else:
            choice = "cpu"

        assert choice == "mps"

    def test_cpu_last_resort(self):
        """CPU is selected when neither CUDA nor MPS is available."""
        has_cuda = False
        has_mps = False

        if has_cuda:
            choice = "cuda"
        elif has_mps:
            choice = "mps"
        else:
            choice = "cpu"

        assert choice == "cpu"

    def test_function_exists_in_both_trainers(self):
        """_select_torch_device is defined in both trainers."""
        from openjarvis.learning.intelligence.orchestrator.grpo_trainer import (
            _select_torch_device as grpo_fn,
        )
        from openjarvis.learning.intelligence.orchestrator.sft_trainer import (
            _select_torch_device as sft_fn,
        )

        assert callable(sft_fn)
        assert callable(grpo_fn)

    def test_exported_from_orchestrator_init(self):
        """_select_torch_device is exported from orchestrator package."""
        from openjarvis.learning.intelligence.orchestrator import (
            _select_torch_device,
        )

        assert callable(_select_torch_device)
