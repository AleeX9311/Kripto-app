# Donchian

Donchian: Preboj razpona privlači tok.

- Edge: Preboj razpona privlači tok.
- Rules: vstop Zaprtje nad N-dnevnim vrhom, stop pod dnom. Izstop: stop ali 48 sveč. Stop: pod strukturo. Velikost: račun × 1% / razdalja do stopa, največ 1x. Parametri: [20].
- Results: donos izven vzorca -0.0263, največji padec -0.2114, posli 284. To so izidi testa, ne obljuba.
- Walk-forward: 15 oken, 12 mesecev učenja in 3 mesece testa. Sharpe 0.001, deflated Sharpe 0.0.
- Baseline: kupi in drži Sharpe 0.004, naključni vstopi Sharpe -0.118.
- Pass: ni prestal.

WHY THIS COULD FAIL: Povratek v razpon.
