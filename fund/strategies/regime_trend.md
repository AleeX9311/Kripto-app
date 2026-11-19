# Režimski trend

Režimski trend: Nad 200-dnevnim povprečjem trend drži.

- Edge: Nad 200-dnevnim povprečjem trend drži.
- Rules: vstop EMA signal samo nad povprečjem. Izstop: stop ali 48 sveč. Stop: pod strukturo. Velikost: račun × 1% / razdalja do stopa, največ 1x. Parametri: [20, 50].
- Results: donos izven vzorca -0.4906, največji padec -0.5496, posli 335. To so izidi testa, ne obljuba.
- Walk-forward: 15 oken, 12 mesecev učenja in 3 mesece testa. Sharpe -0.099, deflated Sharpe 0.0.
- Baseline: kupi in drži Sharpe 0.004, naključni vstopi Sharpe -0.156.
- Pass: ni prestal.

WHY THIS COULD FAIL: Dolg padajoči trg.
