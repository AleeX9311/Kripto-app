# Preboj volatilnosti

Preboj volatilnosti: Miru sledi širitev.

- Edge: Miru sledi širitev.
- Rules: vstop Ozek razpon, preboj, retest. Izstop: stop ali 48 sveč. Stop: pod strukturo. Velikost: račun × 1% / razdalja do stopa, največ 1x. Parametri: [24, 0.06].
- Results: donos izven vzorca -0.3898, največji padec -0.4641, posli 305. To so izidi testa, ne obljuba.
- Walk-forward: 15 oken, 12 mesecev učenja in 3 mesece testa. Sharpe -0.139, deflated Sharpe 0.0.
- Baseline: kupi in drži Sharpe 0.004, naključni vstopi Sharpe -0.114.
- Pass: ni prestal.

WHY THIS COULD FAIL: Lažni preboj v razponu.
