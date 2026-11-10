# Designer evaluation

Run: `/home/francesco/Desktop/MOSAIC/Eval/designer_eval/results/run_20261109_111422`
Queries: 45 diagnostic + 100 smart-city

## Diagnostic set (45)

| Classe | N | Reach | Class. acc. (reached) | End-to-end | Nonempty pre | Nonempty post | Contract yield |
|---|---:|---:|---:|---:|---:|---:|---:|
| OUT_OF_DOMAIN | 15 | 15/15 (100.0%; 95% CI 79.6%–100.0%) | 100.0% | 15/15 (100.0%; 95% CI 79.6%–100.0%) | 0/15 (0.0%; 95% CI 0.0%–20.4%) | 0/15 (0.0%; 95% CI 0.0%–20.4%) | 15/15 (100.0%; 95% CI 79.6%–100.0%) |
| AMBIGUOUS | 15 | 13/15 (86.7%; 95% CI 62.1%–96.3%) | 92.3% | 12/15 (80.0%; 95% CI 54.8%–93.0%) | 2/15 (13.3%; 95% CI 3.7%–37.9%) | 2/15 (13.3%; 95% CI 3.7%–37.9%) | 0/15 (0.0%; 95% CI 0.0%–20.4%) |
| INVALID | 15 | 12/15 (80.0%; 95% CI 54.8%–93.0%) | 91.7% | 11/15 (73.3%; 95% CI 48.0%–89.1%) | 3/15 (20.0%; 95% CI 7.0%–45.2%) | 3/15 (20.0%; 95% CI 7.0%–45.2%) | 1/15 (6.7%; 95% CI 1.2%–29.8%) |
| **Totale** | 45 | 40/45 (88.9%; 95% CI 76.5%–95.2%) | 95.0% | 38/45 (84.4%; 95% CI 71.2%–92.3%) | 5/45 (11.1%; 95% CI 4.8%–23.5%) | 5/45 (11.1%; 95% CI 4.8%–23.5%) | 15/15 (100.0%; 95% CI 79.6%–100.0%) |

`Nonempty pre` = il planner ha prodotto almeno un task (il Designer non è stato raggiunto).
`Nonempty post` = dopo validazione/auto-fix restano task eseguibili.
`Contract yield` è calcolato sulle sole 15 query OOD.

## Contract validity (OOD)

- Contract hard-valid: 15/15 (100.0%; 95% CI 79.6%–100.0%) sui contratti prodotti
- Contract leakage su query non-OOD: 1/30 (3.3%; 95% CI 0.6%–16.7%)

## Confusion matrix

| gold \ pred | OUT_OF_DOMAIN | AMBIGUOUS | INVALID | UNKNOWN | NOT_REACHED | ERROR |
|---|---:|---:|---:|---:|---:|---:|
| OUT_OF_DOMAIN | 15 | 0 | 0 | 0 | 0 | 0 |
| AMBIGUOUS | 0 | 12 | 1 | 0 | 2 | 0 |
| INVALID | 1 | 0 | 11 | 0 | 3 | 0 |

## Smart-city 100 (in-domain)

| Metrica | Valore |
|---|---:|
| Designer activation (FP route) | 2/100 (2.0%; 95% CI 0.6%–7.0%) |
| Designer says OUT_OF_DOMAIN | 1/100 (1.0%; 95% CI 0.2%–5.4%) |
| Contract emitted | 1/100 (1.0%; 95% CI 0.2%–5.4%) |
| Gold op(s) in Ret | 96/100 (96.0%; 95% CI 90.2%–98.4%) |
| FP | gold op in Ret | 1/96 (1.0%; 95% CI 0.2%–5.7%) |
| FP attributable to retrieval | 1/2 (50.0%; 95% CI 9.5%–90.5%) |

## Abstention (45 vs 100)

TP=40 FP=2 FN=5 TN=98 · precision=95.2% · recall=88.9% · F1=92.0%

## Contract plausibility

Nessun punteggio disponibile. Compila `contracts_to_review_A.csv` e `_B.csv`.

## Reuse

- Contract totali: 16
- Contract con almeno un endpoint già nel catalogo: 0/16 (0.0%; 95% CI 0.0%–19.4%)
- Percentuale media di endpoint riusati: 0.0%
- service_id duplicati: 0/16 (0.0%; 95% CI 0.0%–19.4%)

## Failure breakdown

| Set | Gold | Route reason | Count |
|---|---|---|---:|
| diagnostic | AMBIGUOUS | PLANNER_EMPTY_DESIGNER | 13 |
| diagnostic | AMBIGUOUS | PLANNER_NONEMPTY | 2 |
| diagnostic | INVALID | PLANNER_EMPTY_DESIGNER | 12 |
| diagnostic | INVALID | PLANNER_NONEMPTY | 3 |
| diagnostic | OUT_OF_DOMAIN | PLANNER_EMPTY_DESIGNER | 15 |
| smart_city | IN_DOMAIN | PLANNER_EMPTY_DESIGNER | 2 |
| smart_city | IN_DOMAIN | PLANNER_NONEMPTY | 98 |
