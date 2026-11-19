# Vrnitev k povprečju

Vrnitev k povprečju: Prisilni prodajalci plačajo likvidnost.

- Edge: Prisilni prodajalci plačajo likvidnost.
- Rules: vstop Zaprtje globoko pod povprečjem, stop pod zadnjimi minimumi. Izstop: stop ali 48 sveč. Stop: pod strukturo. Velikost: račun × 1% / razdalja do stopa, največ 1x. Parametri: [40, 1.0].
- Results: donos izven vzorca -0.8718, največji padec -0.8719, posli 484. To so izidi testa, ne obljuba.
- Walk-forward: 15 oken, 12 mesecev učenja in 3 mesece testa. Sharpe -0.259, deflated Sharpe 0.0.
- Baseline: kupi in drži Sharpe 0.004, naključni vstopi Sharpe -0.146.
- Pass: ni prestal.

WHY THIS COULD FAIL: Trend, ki se ne vrne.
