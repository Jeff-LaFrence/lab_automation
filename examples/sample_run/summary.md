# Run 20261111T045100Z_ec85: PASS

Input: examples/designs/good_factorial.json  
Assay: generic_reaction_screen_v1    Workcell: ot2_bench (opentrons_ot2)

## What would be run

- 12 conditions x 3 replicates + 4 controls = 40 wells, 200 uL each
- Reagent use: substrate 1600 of 5400 uL usable, solvent 3322 of 12600 uL usable, catalyst 278 of 1080 uL usable, cosolvent 1600 of 9000 uL usable, base 1200 of 7200 uL usable
- Tips: small 38 of 96, large 4 of 96

## Checks

- Design rules: pass
- Plan rules: pass
- Workcell rules: pass
- Simulator (opentrons_ot2): pass, 645 steps
- Ledger: simulated dispenses match the plan for all 40 wells

Files: design.json, plan.json, well_map.csv, worklist.csv, protocol.py, sim_log.txt, ledger.csv  
Design hash: 3ad278127ad2099818310d0fed49d899973fa862c04d3998bdcb0072d9294891  
Protocol hash: 0d9b05e07cb80fe85613c00778b2c6bad51bf642760d950f4e1e54c6b0fc011b
