# VibeVoice-ASR Configuration Investigation Report

## TL;DR

After exhaustive investigation, **your vendor wrapper implementation is faithful to the official Microsoft VibeVoice code.** The prompt template, audio preprocessing, generation parameters, dtype handling, token mapping, and vLLM serving config all match or are functionally equivalent to the official reference. No configuration mistake was found. The observed quality gap vs Whisper V3 appears to be an inherent model characteristic, not a deployment error.

---

## What We Checked (Everything Matches)

| Component | Official | Yours | Verdict |
|---|---|---|---|
| System prompt | `"You are a helpful assistant that transcribes audio input into text output in JSON format."` | Identical | OK |
| User prompt | `"This is a {dur:.2f} seconds audio, please transcribe it with these keys: Start time, End time, Speaker ID, Content"` | Identical | OK |
| Hotwords format | `"with extra info: {hotwords}\n\n"` | Identical | OK |
| Temperature | 0.0 (greedy) | 0.0 | OK |
| Top-p | 1.0 | 1.0 | OK |
| Audio sample rate | 24kHz via FFmpeg | 24kHz via FFmpeg | OK |
| Audio normalization | -25 dBFS | -25 dBFS | OK |
| Audio encoder dtype | bfloat16 (from config) | bfloat16 (from config) | OK |
| LM dtype | bfloat16 | bfloat16 | OK |
| Speech tokens | `<\|object_ref_start\|>`, `<\|box_start\|>`, `<\|object_ref_end\|>` | Same fallback chain | OK |
| Compress ratio | 3200 (from config) | 3200 (from config) | OK |
| Streaming segments | 60s default | 60s default | OK |
| vLLM version | v0.14.1 | v0.14.1 | OK |
| Prefix caching | Disabled | Disabled | OK |
| Chunked prefill | Enabled | Enabled | OK |
| max_tokens (effective) | 32768 (explicit) | ~43000 (vLLM default = max_model_len - input) | Actually MORE generous |
| VibeVoice commit | HEAD of main (`1807b858`) | `1807b858` | Latest available |
| Chat template | Handles `audio_url` -> `<\|AUDIO\|>` | Same template | OK |

---

## Stochastic VAE Sampling (FIXED — Now Deterministic)

The acoustic tokenizer uses **doubly stochastic Gaussian sampling** by default (`modular_vibevoice_tokenizer.py:992`):

```python
# dist_type == 'gaussian', fix_std == 0.5:
value = self.std / 0.8          # 0.5 / 0.8 = 0.625
std = torch.randn(batch_size) * value  # random std ~ N(0, 0.625) per batch
x = self.mean + std * torch.randn_like(self.mean)
```

The noise magnitude *itself* is random — but critically, it is a **single scalar per recording** (`torch.randn(batch_size)` produces shape `[1]` for single-request inference), broadcast across all time steps and feature dimensions. Each request rolls one random draw from N(0, 0.625) that uniformly scales the noise for the entire audio. This means some requests get near-zero noise (clean encoding) while others get noise magnitude >1 (degraded encoding), causing unpredictable per-request quality variance for the same audio input.

The official `modeling_vibevoice_asr.py:255` does the same thing — sampling is the training default. However, the per-request quality lottery is unacceptable for production serving.

**Fix applied:** `ENV VIBEVOICE_USE_MEAN=1` added to the Dockerfile. This makes the audio encoder use `acoustic_out.mean` directly (the VAE's most-likely representation) instead of sampling, via the existing code path at `model.py:238-244`. Same audio in, same embeddings out, every time.

---

## Minor Gaps (Not Quality-Affecting for Most Audio)

### 1. `--max-model-len 48000` vs official 65536
Limits max audio to ~48 minutes instead of ~60 minutes. Only affects very long audio.

### 2. No Repetition Auto-Recovery
The official `test_api_auto_recover.py` implements a client-side retry mechanism:
- Detects repetition loops (10+ repeats of a 10+ char pattern in last 400 chars)
- Retries with escalating temperature (0.2, 0.3, 0.4) and top_p=0.95
- Truncates to last complete JSON segment boundary
- Up to 3 retries

Your server streams raw vLLM output without this detection. For audio with music, SFX, or repetitive speech patterns, this means the client gets garbage output instead of a recovered transcription.

### 3. Known Model-Level Limitations (Not Configuration Issues)
Per GitHub issues and HuggingFace discussions:
- **Repetition loops** on sustained sounds or repetitive words (#227)
- **Timing drift** ~10s over 44-minute segments (#230)
- **Random glitches** in transcription (#HF Discussion 12)
- **Conservative diarization** - undercounts speakers (#HF Discussion 13)
- **One user could not reproduce AMI SDM benchmark results** (#237) - got significantly worse numbers than published

---

## Honest Assessment

If the quality issue is "not even as high as Whisper V3" for **general, short-form, single-speaker ASR**, this may actually be an inherent characteristic of the model rather than a configuration mistake:

1. **VibeVoice is a multi-task model** - it jointly does ASR + diarization + timestamps in structured JSON. This multi-task objective trades off pure ASR accuracy.
2. **VibeVoice uses 7.5 tokens/second** compression (24kHz / 3200). Whisper uses ~50 tokens/second mel features. VibeVoice's aggressive compression may lose information that matters for fine-grained ASR.
3. **The JSON output constraint** forces the model to allocate capacity to formatting, timestamps, and speaker IDs rather than focusing entirely on transcription accuracy.
4. **VibeVoice's strength is long-form** (up to 60 min in one pass with diarization). For short clips where Whisper excels, VibeVoice may not be the better choice.

---

## Recommended Actions

1. **Implement repetition auto-recovery** - Port the logic from `test_api_auto_recover.py` to your server (highest practical impact)
2. **A/B test with native transformers inference** - Run the same audio through the official `vibevoice_asr_inference_from_file.py` demo script to establish whether the quality gap is vLLM-specific or model-inherent
3. **Provide hotwords when available** - Passing domain-relevant vocabulary via the hotwords field genuinely helps ASR accuracy by anchoring the model's vocabulary (the clause is correctly omitted when empty, so no confusion)
4. **Check if upstream has new commits** - Run `cd VibeVoice && git fetch origin && git log HEAD..origin/main --oneline` to see if Microsoft has pushed fixes since your clone

---

## Appendix: Full Parameter Inventory

### vLLM Server Launch (Dockerfile CMD)

```
--served-model-name vibevoice
--host 127.0.0.1
--trust-remote-code
--dtype bfloat16
--max-num-seqs 64
--max-model-len 48000
--gpu-memory-utilization 0.90
--no-enable-prefix-caching
--enable-chunked-prefill
--chat-template-content-format openai
--tensor-parallel-size 1
--port 8000
```

### Environment Variables

| Variable | Value | Purpose |
|---|---|---|
| `VIBEVOICE_FFMPEG_MAX_CONCURRENCY` | 64 | Max concurrent FFmpeg decode processes |
| `PYTORCH_ALLOC_CONF` | `expandable_segments:True` | PyTorch memory allocation strategy |
| `VIBEVOICE_USE_MEAN` | `1` | Disables stochastic VAE sampling, uses deterministic mean |

### Model Architecture

| Component | Value |
|---|---|
| LLM backbone | Qwen2.5-7B (28 layers, 3584 hidden, 28 attn heads, 4 KV heads) |
| Acoustic VAE dim | 64 |
| Semantic VAE dim | 128 |
| Encoder ratios | [8, 5, 5, 4, 2, 2] (product = 3200) |
| Acoustic fix_std | 0.5 (Gaussian sampling) |
| Semantic fix_std | 0 (no sampling, mean only) |
| Token rate | 7.5 Hz (24000 / 3200) |
| Target sample rate | 24000 Hz |
| Connector architecture | fc1 -> RMSNorm -> fc2 |

### Audio Processing Pipeline

1. Raw bytes received from HTTP upload
2. Base64-encoded, sent to vLLM as `data:{mime};base64,{data}` URL
3. vLLM's patched `AudioMediaIO` decodes via FFmpeg stdin pipe
4. Resampled to 24kHz mono
5. Normalized to -25 dBFS via `AudioNormalizer`
6. Stored as raw waveform in `raw_audio` field
7. During forward pass: acoustic VAE + semantic VAE -> connectors -> combined embeddings
8. Embeddings replace `<|box_start|>` (speech_pad) tokens in prompt

### Prompt Token Structure

```
<|im_start|>system
You are a helpful assistant that transcribes audio input into text output in JSON format.<|im_end|>
<|im_start|>user
<|object_ref_start|><|box_start|>...(N times)...<|object_ref_end|>
This is a {duration:.2f} seconds audio, please transcribe it with these keys: Start time, End time, Speaker ID, Content<|im_end|>
<|im_start|>assistant
```

Where N = `ceil(audio_samples / 3200)`.

### Key Source Files

| File | Purpose |
|---|---|
| `Dockerfile` | vLLM container definition and launch flags |
| `server/vllm_client.py` | Prompt construction and vLLM API calls |
| `server/audio.py` | Audio base64 encoding, MIME detection, duration probing |
| `server/config.py` | Server configuration schema |
| `VibeVoice/vllm_plugin/model.py` | Audio encoder, multimodal processor, model class |
| `VibeVoice/vllm_plugin/__init__.py` | Plugin registration and weight mapping |
| `VibeVoice/vllm_plugin/tools/generate_tokenizer_files.py` | Tokenizer generation from Qwen2.5 base |
| `VibeVoice/vibevoice/processor/audio_utils.py` | FFmpeg audio loading and normalization |
| `VibeVoice/vibevoice/processor/vibevoice_asr_processor.py` | Official ASR processor (reference) |
| `VibeVoice/vibevoice/modular/modeling_vibevoice_asr.py` | Official modeling code (reference) |
| `VibeVoice/vibevoice/modular/modular_vibevoice_tokenizer.py` | VAE tokenizer encode/sample methods |
| `VibeVoice/vibevoice/configs/qwen2.5_7b_32k.json` | Model configuration |

### References

- [VibeVoice-ASR on HuggingFace](https://huggingface.co/microsoft/VibeVoice-ASR)
- [VibeVoice GitHub](https://github.com/microsoft/VibeVoice)
- [VibeVoice-ASR Technical Report (arXiv:2601.18184)](https://arxiv.org/abs/2601.18184)
- [VibeVoice Family Technical Report (arXiv:2508.19205)](https://arxiv.org/html/2508.19205v1)
- [Official vLLM ASR docs](https://github.com/microsoft/VibeVoice/blob/main/docs/vibevoice-vllm-asr.md)
