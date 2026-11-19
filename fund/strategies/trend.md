# Sledenje trendu

Sledenje trendu: Čreda in počasne novice držijo trend.

- Edge: Čreda in počasne novice držijo trend.
- Rules: vstop EMA hitro nad počasno, dotik, višji minimum. Izstop: stop ali 48 sveč. Stop: pod strukturo. Velikost: račun × 1% / razdalja do stopa, največ 1x. Parametri: [10, 40].
- Results: donos izven vzorca -0.4687, največji padec -0.55, posli 485. To so izidi testa, ne obljuba.
- Walk-forward: 15 oken, 12 mesecev učenja in 3 mesece testa. Sharpe -0.063, deflated Sharpe 0.0.
- Baseline: kupi in drži Sharpe 0.004, naključni vstopi Sharpe -0.14.
- Pass: ni prestal.

WHY THIS COULD FAIL: Tihi razpon ali sunek, ki prebije minimum.
