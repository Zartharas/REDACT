> **Summary (pre-registered in `PCCF_PHASE10_PREREGISTRATION.md`, part A).**
> **H40 SUPPORTED, but the effect is modest.**
>
> - **Setup:** log telemetry with names in fields and message text, 5
>   held-out sources × 3 seeds, 42 eligible group cells. The static S2
>   baseline reproduces phase 3b exactly (P 0.854, R 0.738).
> - **Floor violations:**
>
>   | setting | violations |
>   |---|---|
>   | static S2 | 3 |
>   | ACI with a 10 % audit sample | 2 (with or without a 500-line feedback delay) |
>   | ACI with full feedback | 0 |
>
> - **Precision** moves by ≤ 0.002 in every setting.
> - **No drift within a stream:** recall in the first and second halves is
>   the same (0.968 / 0.972).
>
> **Reading:** the online adaptive threshold never hurts, and it closes the
> remaining gaps once feedback is complete. With realistic 10 % sampled
> audits, it fixes one of the three misses. A 500-line feedback delay makes
> no difference at this stream length.
>
> Part B (throughput and cost) needs the author's Docker run:
> `validation/real_data/phase10/run_phase10_throughput.sh`.

# PCCF phase 10A results: ACI with audited, delayed analyst feedback (log telemetry)

| method | floor violations (cells) | pooled P | pooled R | R field | R message | cand. recall 1st / 2nd half |
|---|---|---|---|---|---|---|
| STATIC | 3/42 | 0.854 | 0.738 | 0.762 | 0.763 | 0.968 / 0.972 |
| ACI rho=1.0 D=0 | 0/42 | 0.852 | 0.739 | 0.762 | 0.765 | 0.969 / 0.974 |
| ACI rho=0.1 D=0 | 2/42 | 0.853 | 0.738 | 0.762 | 0.764 | 0.968 / 0.973 |
| ACI rho=0.1 D=500 | 2/42 | 0.853 | 0.738 | 0.762 | 0.764 | 0.968 / 0.973 |

=== Verdict (mechanical) ===
  H40: SUPPORTED
