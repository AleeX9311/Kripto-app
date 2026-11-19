# Volatilnostni cilj

Volatilnostni cilj: Stop sledi razponu sveč.

- Edge: Stop sledi razponu sveč.
- Rules: vstop Trend, stop je večkratnik razpona. Izstop: stop ali 48 sveč. Stop: pod strukturo. Velikost: račun × 1% / razdalja do stopa, največ 1x. Parametri: [2.5].
- Results: donos izven vzorca -0.4654, največji padec -0.6283, posli 535. To so izidi testa, ne obljuba.
- Walk-forward: 15 oken, 12 mesecev učenja in 3 mesece testa. Sharpe -0.047, deflated Sharpe 0.0.
- Baseline: kupi in drži Sharpe 0.004, naključni vstopi Sharpe -0.131.
- Pass: ni prestal.

WHY THIS COULD FAIL: Razpon se razširi čez stop.
