"""Main entry point for FluxDM (`python3 -m fluxdm` or `fluxdm`)."""

from __future__ import annotations

import sys
from typing import Optional


def _launch_gui(urls: Optional[list[str]] = None) -> int:
    try:
        from .gui.app import run_gui
    except (ImportError, ValueError) as exc:
        print(
            "Não foi possível iniciar a interface gráfica (GTK4/Libadwaita indisponível).",
            file=sys.stderr,
        )
        print(f"Detalhe: {exc}", file=sys.stderr)
        print(
            "Instale python3-gi, GTK4 e Libadwaita, ou utilize a linha de comando:\n"
            "  fluxdm --help\n"
            "  fluxdm add <URL>",
            file=sys.stderr,
        )
        return 1

    try:
        return int(run_gui(urls=urls or None))
    except Exception as exc:  # noqa: BLE001
        print(
            f"Erro ao iniciar a interface gráfica do FluxDM: {exc}",
            file=sys.stderr,
        )
        print(
            "Verifique se há uma sessão gráfica ativa (DISPLAY/WAYLAND_DISPLAY) "
            "ou utilize o modo CLI: fluxdm --help",
            file=sys.stderr,
        )
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        return _launch_gui()

    if args[0] == "--gui":
        urls = [a for a in args[1:] if not a.startswith("-")]
        return _launch_gui(urls)

    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
