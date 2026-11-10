# Designer evaluation harness

Questa directory contiene l'harness per valutare il fallback **Designer** usato
quando il planner restituisce un piano vuoto.  È un setup nuovo e separato:

- **45 query diagnostiche** nuove, bilanciate 15/15/15:
  - `OUT_OF_DOMAIN`: il catalogo non può soddisfare la richiesta;
  - `AMBIGUOUS`: la richiesta è sotto-specificata o ha più intenti;
  - `INVALID`: la richiesta è logicamente o semanticamente invalida.
- **100 query smart-city in-domain** per misurare i falsi positivi del Designer.
- Due annotatori indipendenti, con foglio di adjudication separato.
- Un runner che usa `dry_run` (planning + validazione, nessuna esecuzione).
- Un analizzatore che produce matrice di confusione, metriche di reach,
  "silent wrong plan", contract yield/validità, riuso, plausibilità e
  falsi positivi.

Le etichette in `queries/designer_queries.csv` sono **proposte** e non
adjudicated.  Non usarle come risultati finali nel paper prima
dell'adjudication.

## Struttura

```
designer_eval/
├── queries/
│   ├── designer_queries.csv          # seed + label proposte (poi adjudicated)
│   └── smart_city_100.csv            # copia normalizzata delle 100 query in-domain
├── annotations/
│   ├── annotator_A.csv               # cieco: solo query_id, query, categoria da compilare
│   ├── annotator_B.csv               # cieco
│   └── adjudicated.csv               # proposta visibile + categoria adjudicated
├── contracts/                        # (opzionale) materiale di scoring
├── results/                          # run_*/
├── run_designer_eval.py
├── analyze_designer_eval.py
├── prepare_annotation.py
├── apply_adjudication.py
├── fetch_catalog_snapshot.py
├── common.py
└── README.md
```

## Prerequisiti

1. Stack MOSAIC avviato (control unit, gateway, catalogo, Ollama).
2. Control unit aggiornato con il supporto `dry_run` e i campi di routing
   (`route_reason`, `planner_status`, `planner_tasks_count`,
   `validated_tasks_count`, `designer_attempted`).  I campi sono additivi.
3. Snapshot del catalogo completo, per HC-1 e per il riuso esatto degli
   endpoint:

```bash
python3 test_validation/designer_eval/fetch_catalog_snapshot.py \
  --catalog-url http://localhost:5000 \
  --out test_validation/designer_eval/catalog_snapshot.json
```

Se lo snapshot manca, il runner funziona ma HC-1 e reuse sono calcolati solo
sui servizi recuperati; l'analisi lo segnala.

## Annotazione e adjudication

I fogli ciechi sono già stati generati.  Se devi rigenerarli:

```bash
python3 test_validation/designer_eval/prepare_annotation.py --force
```

Protocollo:

1. `annotator_A.csv` e `annotator_B.csv` contengono solo `query_id`, `query` e
   colonne vuote.  Ogni annotatore assegna una delle tre categorie e una
   confidenza 1–5, senza vedere le etichette proposte.
2. `adjudicated.csv` contiene anche `proposed_category`; i due annotatori
   discutono i disaccordi e compilano `adjudicated_category`.
3. Copiare le etichette finali in `queries/designer_queries.csv`, aggiornando
   `gold_category` e impostando `label_status=adjudicated` per tutte le 45
   righe.  Il runner avvisa se il file non è adjudicated.

   In alternativa, dopo aver compilato `annotations/adjudicated.csv`:

   ```bash
   python3 test_validation/designer_eval/apply_adjudication.py
   # -> queries/designer_queries_adjudicated.csv
   ```

   Poi passare `--queries queries/designer_queries_adjudicated.csv` al runner
   e all'analisi.  Se il file `queries/designer_queries_adjudicated.csv`
   esiste, runner e analisi lo usano automaticamente come default.
   Usare `--in-place` solo quando l'adjudication è definitiva.

Non usare le 30 query di `test_validation/out_of_plan_test_requests.csv` come
test set: sono state usate per il debug e hanno etichette di un solo
annotatore.  Restano utili solo come pilota.

## Esecuzione

Dry-run (raccomandato; non esegue chiamate write-side):

```bash
# Per un run riproducibile (opzionale, ma consigliato per il paper)
DESIGNER_TEMPERATURE=0.0 DESIGNER_SEED=42 docker compose up -d control-unit

python3 test_validation/designer_eval/run_designer_eval.py \
  --control-url http://localhost:5500/api/control/invoke \
  --catalog test_validation/designer_eval/catalog_snapshot.json \
  --dry-run \
  --out-dir test_validation/designer_eval/results/run_manual
```

`--dry-run` è il default.  Usa `--no-dry-run` solo se vuoi eseguire i piani e
accetti effetti sui mock.

Per un test rapido:

```bash
python3 test_validation/designer_eval/run_designer_eval.py --limit 5 --allow-proposed
```

Il runner salva:

- `raw_responses.jsonl`: risposta completa, una riga per query;
- `per_query.csv`: riga piatta con routing, contratto, retrieval;
- `run_manifest.json`: configurazione, commit, modello, hash dei prompt,
  timestamp.

## Analisi

```bash
python3 test_validation/designer_eval/analyze_designer_eval.py \
  --run-dir test_validation/designer_eval/results/run_manual
```

Output principali:

- `summary.md`: tabelle leggibili;
- `summary.json`: tutte le metriche, incluse Wilson CI;
- `confusion_matrix.csv`;
- `failure_breakdown.csv` (route reason per classe);
- `per_query_enriched.csv`;
- `contracts_to_review_A.csv` e `_B.csv`: contratti dei soli OOD, con
  `rationale` rimosso, da valutare alla cieca.

### Scoring dei contratti

Ogni valutatore compila una copia di `contracts_to_review_A.csv` /
`_B.csv` con:

- `score`: plausibilità del contratto, intero 1–5;
- `reuse_semantic`: `yes`/`no` se il contratto riusa un'operazione o una
  capability del catalogo (riuso semantico, distinto dal match esatto);
- `notes`: motivazione breve.

Rubrica 1–5:

| Score | Significato |
|---:|---|
| 1 | Contratto inutilizzabile: servizio/endpoint incoerenti o fuori dominio. |
| 2 | Idea debole: endpoint o schema non bastano a soddisfare la richiesta. |
| 3 | Usabile con correzioni moderate: struttura corretta, dettagli da fissare. |
| 4 | Buono: servizio ed endpoint plausibili, schema quasi completo. |
| 5 | Direttamente implementabile: servizio, endpoint e schema coerenti. |

Poi:

```bash
python3 test_validation/designer_eval/analyze_designer_eval.py \
  --run-dir ... \
  --scores-a .../contract_scores_A.csv \
  --scores-b .../contract_scores_B.csv
```

L'analisi calcola media/mediana, quota di contratti con media ≥ 3, accordo
esatto, accordo entro 1 punto, weighted kappa quadratico e ICC(2,1).

## Metriche

Per le 45 query:

- **Reach rate**: quota di query in cui il Designer viene attivato.
- **Classification accuracy (reached)**: accuratezza della categoria tra le
  query raggiunte.
- **End-to-end accuracy**: accuratezza contando anche i piani non vuoti come
  errore.
- **Silent wrong plan (pre)**: quota di query con almeno un task del planner
  prima della validazione.  È il fallimento di routing più importante: il
  Designer non viene chiamato.
- **Silent wrong plan (post)**: quota di query con task eseguibili dopo
  validazione/auto-fix.
- **Contract yield**: quota di OOD con un contratto prodotto.
- **Contract hard validity**: rispetto di HC-1, HC-2, HC-3, HC-4, HC-5.
- **Contract leakage**: contratti prodotti su query non-OOD (deve essere 0).

Per le 100 query smart-city:

- **Designer activation**: quota di query in-domain in cui il Designer viene
  attivato (falso positivo di routing).
- **Predicted OOD**: quota classificata `OUT_OF_DOMAIN`.
- **FP | gold op in Ret**: falsi positivi condizionati alla copertura del
  retrieval.  Separa gli errori del Designer dagli errori di retrieval.
- **FP attributable to retrieval**: attivazioni in cui il gold endpoint non era
  tra i servizi recuperati.

Complessivamente:

- **Abstention precision/recall/F1**: il Designer come classificatore
  "astenersi" sulle 45 + 100 query.

## Test dell'harness

```bash
python3 -m unittest discover -s test_validation/designer_eval/tests -v
```

I test coprono bilanciamento del dataset, normalizzazione endpoint, HC-1/HC-5,
`ret_has_oracle` e la lettura dei flag booleani dal CSV.

## Caveat importanti

- **Prompt contamination**: il prompt del Designer è stato ripulito
  dall'esempio `smart-library-mock` e dall'esempio "energy", sostituiti con un
  esempio neutro di loan museale.  Restano invece contaminati gli esempi del
  planner (`smart-library-mock`, `smart-hospital-mock`, `smart-campus-mock`,
  `smart-logistics-mock`, `smart-hospitality-mock`, `smart-acoustics-mock`,
  `smart-real-estate-mock`), che non sono nel catalogo smart-city.  La
  decontaminazione del planner resta necessaria prima della versione finale e
  richiede un rerun del planner.
- **Designer input**: il Designer riceve solo i servizi recuperati
  (`discovered["services"]`), non l'intero catalogo.  La metrica
  `ret_has_gold` serve a distinguere un errore del Designer da un errore di
  retrieval.  Una condizione "full catalog" non è implementata in questo
  harness.
- **Modello**: i risultati correnti del sistema usano
  `Qwen3-Embedding-0.6B + bge-reranker-base`; non attribuire le metriche a
  `BGE-small-en-v1.5` senza un rerun.
- **Designer temperature**: hardcoded a `0.2` nel codice originale; ora è
  possibile impostare `DESIGNER_TEMPERATURE` e `DESIGNER_SEED`.  Per una
  valutazione riproducibile usare `DESIGNER_SEED` (e, se si vuole eliminare
  la variabilità residua, `DESIGNER_TEMPERATURE=0.0`).  Compose li passa già
  al container `control-unit`.
- **Routing**: il Designer viene attivato solo se `tasks == []`.  Un JSON
  malformato o un piano non vuoto non lo attivano; l'analisi espone entrambi i
  casi con `route_reason`.
- **Vecchio run giugno**: 26/30 corretto, ma contaminato dagli esempi del
  prompt e non paper-ready.  `Q21`, `Q29`, `Q30` bypassano il Designer; `Q25`
  è classificata `OUT_OF_DOMAIN` invece di `INVALID`.

## Comandi utili

```bash
# Verifica rapida del dataset
python3 - <<'PY'
from test_validation.designer_eval.common import read_csv
from collections import Counter
rows = read_csv('test_validation/designer_eval/queries/designer_queries.csv')
print(Counter(r['gold_category'] for r in rows))
PY

# Analisi dell'ultimo run
python3 test_validation/designer_eval/analyze_designer_eval.py
```
