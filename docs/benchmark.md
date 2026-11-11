# Closed loop benchmark

Question: how many rounds does each strategy need to recommend a near optimal condition? The surfaces and the LC/MS readings are synthetic.

After each round, the recommendation is the tested condition with the highest measured mean. It is scored by its true yield on the noise free surface. Each seed is a different synthetic surface (surface seed = seed). Round 1 is the same Latin hypercube for every strategy on a seed. 8 conditions x 3 replicates per round, noise sd 2.5, injection failures on.

This run used the fast path: the plan's volumes stand in for the simulator's ledger, and compile and simulate are skipped. `labauto loop` runs the full path.

Command: `labauto benchmark --strategies random,optimizer,model --seeds 30 --model-seeds 3 --rounds 6`

## Mean true yield of the recommendation (%)

| Strategy | Seeds | Round 1 | Round 2 | Round 3 | Round 4 | Round 5 | Round 6 |
|---|---|---|---|---|---|---|---|
| random | 30 | 59.7 | 64.5 | 68.5 | 71.7 | 73.7 | 76.9 |
| optimizer | 30 | 59.7 | 66.7 | 74.6 | 81.0 | 81.7 | 81.8 |

## Share of seeds within 3 points of the optimum

| Strategy | Seeds | Round 1 | Round 2 | Round 3 | Round 4 | Round 5 | Round 6 |
|---|---|---|---|---|---|---|---|
| random | 30 | 0.07 | 0.17 | 0.20 | 0.30 | 0.30 | 0.37 |
| optimizer | 30 | 0.07 | 0.27 | 0.43 | 0.80 | 0.97 | 1.00 |

Mean true optimum over the 30 surfaces: 82.3%.

![Benchmark](img/benchmark.png)

- model: not run, no model endpoint configured; live results pending.
- random: 1 s for 30 seeds.
- optimizer: 157 s for 30 seeds.

Model: none (no model strategy was run)  
Date: 2026-11-11  
Git commit: b306dfd
