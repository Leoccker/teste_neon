#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

XDG_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"

BIN_DIR="$HOME/.local/bin"
BIN_LINK="$BIN_DIR/fluxdm"
APP_ROOT="$XDG_DATA_DIR/fluxdm"
APP_DIR="$APP_ROOT/app"
APPS_DIR="$XDG_DATA_DIR/applications"
DESKTOP_DEST="$APPS_DIR/fluxdm.desktop"
ICON_THEME_DIR="$XDG_DATA_DIR/icons/hicolor"
ICON_DIR="$ICON_THEME_DIR/scalable/apps"
ICON_DEST="$ICON_DIR/fluxdm.svg"

usage() {
    cat <<'EOF'
Uso: ./install.sh [opção]

Instalador local do FluxDM para o usuário atual (não requer sudo).

Opções:
  (nenhuma)     Instala ou atualiza o FluxDM em ~/.local
  --uninstall   Remove a instalação do FluxDM de ~/.local
  -h, --help    Exibe esta ajuda
EOF
}

update_desktop_caches() {
    if command -v update-desktop-database >/dev/null 2>&1 && [[ -d "$APPS_DIR" ]]; then
        update-desktop-database -q "$APPS_DIR" >/dev/null 2>&1 || true
    fi

    if [[ -d "$ICON_THEME_DIR" ]]; then
        if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
            gtk4-update-icon-cache -q -t -f "$ICON_THEME_DIR" >/dev/null 2>&1 || true
        elif command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -q -t -f "$ICON_THEME_DIR" >/dev/null 2>&1 || true
        fi
    fi
}

check_runtime() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Erro: python3 não encontrado no PATH. Instale o Python 3.10+ para continuar." >&2
        exit 1
    fi

    if ! python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')" >/dev/null 2>&1; then
        echo "Aviso: GTK4 / Libadwaita (PyGObject) não detectados neste Python."
        echo "       O modo CLI funcionará normalmente; para a interface gráfica, instale"
        echo "       python3-gi, gir1.2-gtk-4.0 e gir1.2-adw-1 (ou equivalentes da sua distro)."
    fi
}

install_app() {
    if [[ ! -d "$SCRIPT_DIR/fluxdm" ]] || [[ ! -f "$SCRIPT_DIR/bin/fluxdm" ]]; then
        echo "Erro: execute este script a partir do diretório raiz do projeto FluxDM." >&2
        exit 1
    fi

    check_runtime

    echo "Instalando FluxDM para o usuário '$USER'..."

    mkdir -p "$BIN_DIR" "$APP_ROOT" "$APPS_DIR" "$ICON_DIR"

    # Instala os arquivos da aplicação de forma limpa usando diretório temporário
    local stage_dir
    stage_dir="$(mktemp -d "${APP_ROOT}/.install-stage.XXXXXX")"
    trap 'rm -rf -- "$stage_dir"' EXIT

    cp -R "$SCRIPT_DIR/fluxdm" "$stage_dir/fluxdm"
    mkdir -p "$stage_dir/bin" "$stage_dir/data/icons"
    cp "$SCRIPT_DIR/bin/fluxdm" "$stage_dir/bin/fluxdm"
    cp "$SCRIPT_DIR/data/fluxdm.desktop" "$stage_dir/data/fluxdm.desktop"
    cp "$SCRIPT_DIR/data/icons/fluxdm.svg" "$stage_dir/data/icons/fluxdm.svg"

    # Remove artefatos de bytecode que possam existir no repositório
    find "$stage_dir" -type d -name '__pycache__' -exec rm -rf -- {} +
    find "$stage_dir" -type f -name '*.py[co]' -delete

    chmod 755 "$stage_dir/bin/fluxdm"

    rm -rf -- "$APP_DIR"
    mv "$stage_dir" "$APP_DIR"
    trap - EXIT

    # Cria link simbólico executável em ~/.local/bin/fluxdm
    ln -sfn "$APP_DIR/bin/fluxdm" "$BIN_LINK"

    # Instala ícone vetorial SVG
    install -m 644 "$APP_DIR/data/icons/fluxdm.svg" "$ICON_DEST"

    # Instala arquivo .desktop apontando para o executável instalado
    local escaped_bin
    escaped_bin="$(printf '%s\n' "$BIN_LINK" | sed 's/[&|\\]/\\&/g')"
    sed "s|^Exec=fluxdm |Exec=${escaped_bin} |" "$APP_DIR/data/fluxdm.desktop" > "$DESKTOP_DEST"
    chmod 644 "$DESKTOP_DEST"

    update_desktop_caches

    echo
    echo "FluxDM instalado com sucesso!"
    echo "  Executável : $BIN_LINK"
    echo "  Aplicação  : $APP_DIR"
    echo "  Atalho     : $DESKTOP_DEST"
    echo "  Ícone      : $ICON_DEST"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            echo
            echo "Aviso: '$BIN_DIR' não está no seu PATH atual."
            echo "Adicione a linha abaixo ao seu ~/.bashrc ou ~/.zshrc:"
            echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
            ;;
    esac
}

uninstall_app() {
    echo "Removendo instalação local do FluxDM..."

    if [[ -L "$BIN_LINK" || -f "$BIN_LINK" ]]; then
        rm -f -- "$BIN_LINK"
        echo "  Removido: $BIN_LINK"
    fi

    if [[ -d "$APP_DIR" ]]; then
        rm -rf -- "$APP_DIR"
        echo "  Removido: $APP_DIR"
    fi

    if [[ -f "$DESKTOP_DEST" ]]; then
        rm -f -- "$DESKTOP_DEST"
        echo "  Removido: $DESKTOP_DEST"
    fi

    if [[ -f "$ICON_DEST" ]]; then
        rm -f -- "$ICON_DEST"
        echo "  Removido: $ICON_DEST"
    fi

    # Remove $APP_ROOT apenas se estiver vazio (preserva banco de dados do usuário)
    rmdir -- "$APP_ROOT" 2>/dev/null || true

    update_desktop_caches

    echo
    echo "FluxDM desinstalado com sucesso."
    echo "Nota: suas configurações e histórico de downloads foram preservados em:"
    echo "  Configurações : $XDG_CONFIG_DIR/fluxdm"
    echo "  Dados         : $APP_ROOT"
}

main() {
    case "${1:-}" in
        ""|--install)
            install_app
            ;;
        --uninstall)
            uninstall_app
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Opção desconhecida: $1" >&2
            echo >&2
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
