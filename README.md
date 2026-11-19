# FluxDM

**FluxDM** é um gerenciador de downloads rápido, leve e moderno para Linux, escrito em Python puro (biblioteca padrão no núcleo e na CLI) com interface gráfica nativa em **GTK4 + Libadwaita** e interface de linha de comando completa.

---

## Recursos Principais

- **Downloads segmentados em paralelo**: divide arquivos automaticamente em múltiplos segmentos simultâneos via cabeçalhos `HTTP Range` para maximizar a largura de banda.
- **Pausar e retomar com segurança**: estado dos segmentos e arquivo temporário `.fluxdm.part` persistidos em SQLite (modo WAL), permitindo retomar downloads mesmo após fechar o programa ou interromper com `Ctrl+C`.
- **Verificação de integridade (SHA-256)**: calcula o hash SHA-256 ao concluir e valida contra um checksum esperado (`--checksum`).
- **Controle de velocidade**: limite de banda global ou por download (em KB/s).
- **Cabeçalhos HTTP personalizados**: suporte a múltiplos cabeçalhos (`-H "Chave: Valor"`), útil para autenticação ou cookies.
- **Interface gráfica moderna (GTK4 + Libadwaita)**: janela adaptativa, barra de progresso detalhada, visualização de segmentos e notificações de desktop.
- **CLI completa com barra de progresso em tempo real**: saída ANSI limpa no terminal e degradação graciosa em pipelines/redirecionamentos sem TTY.
- **Integração Linux (Freedesktop)**: lançador `.desktop`, ícone SVG escalável e suporte aos diretórios padrão XDG.

---

## Requisitos

### Núcleo e CLI
- **Linux**
- **Python 3.10+** (nenhuma dependência externa necessária para o motor de downloads ou a CLI)

### Interface Gráfica (GUI)
- **PyGObject (`gi`)**
- **GTK 4** (`gir1.2-gtk-4.0`)
- **Libadwaita 1** (`gir1.2-adw-1`)

#### Instalação das dependências da GUI por distribuição

**Debian / Ubuntu / Linux Mint / Pop!_OS:**
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

**Fedora:**
```bash
sudo dnf install python3-gobject gtk4 libadwaita
```

**Arch Linux / Manjaro:**
```bash
sudo pacman -S python-gobject gtk4 libadwaita
```

---

## Instalação

### Instalação local do usuário (sem `sudo`)

Execute o instalador na raiz do projeto:

```bash
./install.sh
```

O script instala o FluxDM exclusivamente na pasta do usuário:
- Executável: `~/.local/bin/fluxdm`
- Arquivos da aplicação: `~/.local/share/fluxdm/app`
- Atalho do menu de aplicativos: `~/.local/share/applications/fluxdm.desktop`
- Ícone vetorial: `~/.local/share/icons/hicolor/scalable/apps/fluxdm.svg`

> **Dica:** Certifique-se de que `~/.local/bin` esteja no seu `PATH`. Caso não esteja, adicione `export PATH="$HOME/.local/bin:$PATH"` ao seu `~/.bashrc` ou `~/.zshrc`.

### Desinstalação

```bash
./install.sh --uninstall
```

A desinstalação remove os binários, atalho e ícone instalados, preservando suas configurações e histórico de downloads do usuário.

### Execução direta (sem instalar)

```bash
./bin/fluxdm --help
# ou
python3 -m fluxdm --help
```

---

## Uso

### Interface Gráfica (GUI)

Abrir a janela principal:
```bash
fluxdm
# ou
fluxdm gui
```

Abrir a GUI já pré-preenchendo uma ou mais URLs:
```bash
fluxdm gui https://exemplo.org/arquivo.iso
```

---

### Linha de Comando (CLI)

#### Baixar arquivos (`add`)
```bash
# Download imediato com barra de progresso ao vivo
fluxdm add https://exemplo.org/arquivo.iso

# Definir pasta de destino, nome do arquivo, segmentos e limite de velocidade (KB/s)
fluxdm add https://exemplo.org/imagem.iso \
  -d ~/ISOs \
  -o distro.iso \
  -s 8 \
  -l 2048

# Validar checksum SHA-256 e enviar cabeçalhos HTTP extras
fluxdm add https://exemplo.org/pacote.tar.gz \
  --checksum "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" \
  -H "Authorization: Bearer SEU_TOKEN"

# Apenas adicionar à fila (pausado), sem iniciar agora
fluxdm add https://exemplo.org/video.mp4 --no-start
```

> Pressionar `Ctrl+C` durante um download ativo na CLI pausa o download de forma limpa, permitindo retomá-lo depois com `fluxdm resume <id>`.

#### Inspecionar uma URL antes de baixar (`probe`)
```bash
fluxdm probe https://exemplo.org/arquivo.iso
```
Exibe a URL final (após redirecionamentos), nome detectado, tamanho, suporte a `Range`, `Content-Type` e `ETag`.

#### Listar downloads (`list`)
```bash
# Tabela formatada com todos os downloads
fluxdm list

# Filtrar por status (all, downloading, queued, paused, completed, error, cancelled)
fluxdm list --status paused

# Saída estruturada em JSON
fluxdm list --json
```

#### Ver detalhes completos e segmentos (`info`)
Aceita o ID completo ou um prefixo curto (ex.: os 6–8 primeiros caracteres mostrados em `fluxdm list`):
```bash
fluxdm info a1b2c3d4
```

#### Pausar e retomar downloads (`pause` / `resume`)
```bash
fluxdm pause a1b2c3d4
fluxdm resume a1b2c3d4
```

#### Remover ou limpar histórico (`remove` / `clear`)
```bash
# Remove apenas do banco de dados
fluxdm remove a1b2c3d4

# Remove do banco de dados e apaga o arquivo no disco
fluxdm remove a1b2c3d4 --delete-file

# Remove todos os downloads concluídos do histórico
fluxdm clear
```

#### Configurações persistentes (`config`)
```bash
# Visualizar configurações atuais
fluxdm config --show

# Atualizar preferências
fluxdm config \
  --dir ~/Downloads \
  --max-concurrent 4 \
  --segments 8 \
  --speed-limit 0 \
  --notifications on \
  --user-agent "FluxDM/1.0 (Linux)"
```

---

## Arquitetura

```text
.
├── bin/
│   └── fluxdm                # Script executável de entrada
├── data/
│   ├── fluxdm.desktop        # Atalho Freedesktop (.desktop)
│   └── icons/
│       └── fluxdm.svg        # Ícone vetorial 128x128
├── fluxdm/
│   ├── __init__.py           # Versão e metadados do aplicativo
│   ├── __main__.py           # Roteador de entrada (GUI padrão / CLI com argumentos)
│   ├── models.py             # Modelos de dados (DownloadItem, SegmentInfo, AppConfig)
│   ├── storage.py            # ConfigStore (JSON) e DownloadDatabase (SQLite WAL)
│   ├── engine.py             # Motor HTTP/HTTPS segmentado e gerenciador de fila
│   ├── notifications.py      # Notificações nativas de desktop
│   ├── cli.py                # Interface de linha de comando (argparse + barra ANSI)
│   └── gui/                  # Interface gráfica GTK4 + Libadwaita
├── tests/                    # Suíte de testes automatizados (unittest)
├── install.sh                # Instalador/desinstalador local (~/.local)
└── README.md
```

### Diretórios de dados e configuração (XDG)
- **Configuração**: `${XDG_CONFIG_HOME:-~/.config}/fluxdm/config.json`
- **Banco de dados**: `${XDG_DATA_HOME:-~/.local/share}/fluxdm/downloads.db`

---

## Testes

Execute a suíte de testes automatizados com `unittest`:

```bash
python3 -m unittest discover -s tests -v
```
