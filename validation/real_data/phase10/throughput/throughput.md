| layer | lines/s | ms/line | vCPU-s per 1M lines | USD per 1M lines* |
|---|---|---|---|---|
| regex / flattened dictionary (detect.scan_flattened) | 98542.9 | 0.01 | 10 | 0.00 |
| spaCy en_core_web_lg NER via Presidio (detect.scan_ner) | 145.1 | 6.89 | 6,890 | 0.08 |
| spaCy NER + beam confidence (as used by PCCF) | 123.7 | 8.08 | 8,085 | 0.09 |
| GLiNER multi v2.1 (zero-shot, threshold 0.3) | 2.9 | 348.75 | 348,752 | 3.88 |
| XLM-R token classification (Davlan/xlm-roberta-base-ner-hrl) | 8.5 | 117.82 | 117,818 | 1.31 |
| PCCF filter (candidates + KV features + rule score + threshold) | 509106.4 | 0.00 | 2 | 0.00 |

*Assumes $0.04/vCPU-hour, single thread, 2000 T7 telemetry lines. Illustration only.
