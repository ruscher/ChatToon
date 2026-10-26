# ChatToon 3.0.1

**Um coapresentador 3D para a sua live.** O ChatToon é um personagem animado que lê o chat do
YouTube, da Twitch, do TikTok e de outras plataformas **ao mesmo tempo**, responde com humor
próprio — mas **primeiro a resposta certa** — e fala com voz de verdade. A inteligência roda no
seu computador com o **Ollama** e a voz vem do **TTS BigLinux**: sem nuvem, sem mensalidade.
Ele aparece no **OBS Studio** com fundo transparente ou **numa janela na área de trabalho**.

> **Autor:** Pedro Bezerra — canal **InfoRecarga** ([youtube.com/@pedro_bezerra](https://www.youtube.com/@pedro_bezerra))
> **Colaborador:** Rafael Ruscher ([github.com/ruscher](https://github.com/ruscher))
> Voluntários do projeto [BigLinux](https://www.biglinux.com.br) · Software livre, GPL-3.0-or-later

![O elenco: Minhoca, E.T., Tux de cartola, Gata, Coruja e Cachorro](docs/screenshots/cast.webp)

| O personagem respondendo um espectador | O relatório ao clicar em Iniciar |
|---|---|
| ![Painel com o Tux falando](docs/screenshots/dashboard-speaking.webp) | ![Relatório da sessão](docs/screenshots/start-report.webp) |
| **Inteligência artificial: modelos instalados, em uso e sugeridos** | **Voz: mecanismos do TTS BigLinux** |
| ![Página de inteligência artificial](docs/screenshots/ai.webp) | ![Página de voz](docs/screenshots/voice.webp) |
| **Todos os chats num só lugar** | **Onde mostrar: OBS, janela ou os dois** |
| ![Chat ao vivo](docs/screenshots/live-chat.webp) | ![Onde mostrar](docs/screenshots/where-to-show.webp) |

<sub>Capturas reais da versão 3.0.0, com dados de demonstração e respostas geradas pelo
`qwen3.5:2b` local.</sub>

**Sumário:** [Recursos](#recursos) · [Instalação](#instalação) · [Primeiros passos](#primeiros-passos) ·
[OBS Studio](#no-obs-studio) · [Janela no computador](#numa-janela-no-computador) ·
[Plataformas](#plataformas) · [Configurações](#configurações-principais) ·
[Problemas](#solução-de-problemas) · [Privacidade](#segurança-e-privacidade) ·
[Desenvolvimento](#desenvolvimento) · [História](#a-história) · [Créditos](#autoria-licença-e-créditos)

---

## Recursos

- **Seis personagens 3D** feitos por código (Three.js): **Minhoca** irônica, **E.T.** curioso,
  **Tux** de cartola, **Gata** sarcástica, **Coruja** sabichona e **Cachorro** animado — cada um
  com personalidade, voz, gestos e humores próprios, expressões, piscadas, olhar e **boca que se
  mexe com o áudio real**.
- **Respostas corretas antes do humor**: contas calculadas com exatidão, folha de referência de
  Linux e computação, verificação de cada resposta. Num conjunto de teste, o `qwen3.5:2b` local
  acerta **89,6 %** das perguntas verificáveis (eram 29 % na 2.2) e o `llama3.1:8b`, 97 %.
- **Memória da conversa** por espectador e plataforma, **emoções** que acompanham o assunto,
  **falas espontâneas** quando o chat fica quieto e **contexto da live** sugerido a partir das
  próprias plataformas — sempre com a sua aprovação.
- **Várias plataformas ao mesmo tempo**, com fila justa: o chat mais cheio não leva todas as
  respostas. **Uma voz de cada vez, sempre.**
- **Sem terminal**: instala e controla o **Ollama**, baixa modelos com progresso, instala
  mecanismos e vozes do **TTS BigLinux** — tudo pela interface, pedindo a senha só quando o
  sistema exige.
- **No OBS ou na área de trabalho**: overlay transparente para a fonte Navegador do OBS (neste
  computador, na rede de casa ou pela internet) e uma janela transparente com o personagem sobre
  as suas janelas.
- **Feito para aparecer na live**: o **Modo Privacidade para Lives** esconde endereços, IPs e
  códigos secretos em todas as telas.
- **Relatório ao Iniciar**: confere IA, voz, plataformas e onde o personagem aparece, só entra
  ao vivo se o essencial estiver pronto e mostra o que ele vai fazer.
- Interface em inglês e **português do Brasil**, temas claro, escuro ou do sistema, modo simples
  e avançado, atalhos de teclado, perfis de transmissão.

## Instalação

### BigLinux, Manjaro e Arch

```bash
git clone https://github.com/ruscher/ChatToon.git
cd ChatToon/packaging/arch
makepkg -si
```

Abra **ChatToon** pelo menu. `chattoon --version` mostra `chattoon 3.0.1`. Para remover:
`sudo pacman -R chattoon` (configurações e memórias ficam em `~/.config/chattoon` e
`~/.local/share/chattoon`).

| Tipo | Pacotes |
|---|---|
| Obrigatórios | `python`, `pyside6`, `python-aiohttp`, `qt6-declarative`, `qt6-svg`, `qt6-webengine`, `iproute2`, `hicolor-icon-theme` |
| Recomendados | `tts-biglinux` (voz), `ollama` (IA — o ChatToon instala pela interface), `pipewire-audio` (áudio no desktop), `obs-studio`, `polkit` (instalar e iniciar pela interface) |
| Opcionais | `biglinux-kokoro-tts` (vozes neurais Kokoro), `python-gobject` + `libsecret` (logins no chaveiro do sistema), `python-cryptography` (webhooks oficiais do Kick), `python-numpy` (conversão de áudio mais rápida), `caddy` (overlay pela internet) |
| TikTok | `chattoon extras install tiktok` — instala a biblioteca TikTokLive numa pasta privada |

**Requisitos:** Linux com Qt 6 e Python 3.11+. Testado no BigLinux com KDE Plasma 6 (Wayland).
A IA roda bem com 8 GB de memória e um modelo pequeno; com uma placa de vídeo dedicada, modelos
de 7–8 bilhões de parâmetros respondem tão rápido quanto os pequenos.

### Direto do código-fonte

```bash
sudo pacman -S --needed pyside6 python-aiohttp qt6-declarative qt6-svg qt6-webengine
python -m chattoon            # o aplicativo
python -m chattoon doctor     # verifica Ollama, TTS BigLinux, áudio e plataformas
```

## Primeiros passos

O assistente da primeira execução guia tudo isto em seis etapas, mostrando em cada uma o que já
está pronto:

1. **Personagem** — escolha um dos seis.
2. **IA** — se o Ollama não estiver instalado, clique em **Instalar Ollama** (repositórios
   oficiais, com aceleração recomendada para a sua placa de vídeo), depois **Iniciar** e baixe um
   modelo sugerido. O modelo em uso fica com **borda verde**.
3. **Voz** — escolha um mecanismo pronto (Piper, Kokoro, RHVoice ou espeak-ng) ou instale um;
   **Ouvir demonstração** mostra como o personagem vai soar.
4. **Onde mostrar** — OBS Studio, janela no computador ou os dois.
5. **Teste** no modo **Demonstração**: escreva no Painel como se fosse alguém do chat.
6. **Plataformas** — em *Plataformas de streaming*, cole o link da live ou o `@canal`, teste a
   conexão, mude para **Assistido** (você aprova cada resposta) ou **Automático** e clique em
   **Iniciar** (`Ctrl+Espaço`).

## No OBS Studio

Em **Onde mostrar → Onde está o seu OBS Studio?**:

- **Neste computador** — copie o endereço; no OBS, **Fontes → + → Navegador**, cole em URL,
  1920 × 1080, deixe *Desligar fonte quando não visível* desmarcado.
- **Outro computador na mesma rede** — permita o acesso pela rede de casa (só computadores da
  mesma rede são atendidos), libere o firewall se o ChatToon avisar e cole o endereço da rede
  no OBS do outro computador.
- **Outro computador pela internet** — o assistente guia domínio, roteador ou túnel e HTTPS, e
  testa o caminho inteiro. Passo a passo em [docs/remote-access.md](docs/remote-access.md).

Para a voz sair pelo OBS: **Voz → Saída de áudio → Dentro do OBS** e, na fonte, *Controlar áudio
via OBS*. Caso contrário, o ChatToon toca a voz no computador (capture com *Áudio do desktop*).

## Numa janela no computador

![O Tux na área de trabalho](docs/screenshots/desktop-window.webp)

Escolha **Janela no computador** (ou **Os dois**) em *Onde mostrar*. Só o personagem e o balão
aparecem, com fundo transparente. Arraste para mover, **Ctrl + rolagem** para mudar o tamanho e
**botão direito** para o painel: trocar de personagem, tamanho, manter acima, travar, balão,
cliques atravessando, mudar de tela, abrir o ChatToon ou sair. A janela nunca toca som: a voz
sai uma vez só, no computador ou no OBS.

| Recurso | KDE Plasma (Wayland e X11) | Outros ambientes X11 | Outros ambientes Wayland |
|---|---|---|---|
| Transparência, arrastar, redimensionar, cliques atravessando | sim | sim (com compositor) | sim |
| Manter acima, posição lembrada, mudar de tela, ocultar da barra | sim | sim | não (o compositor decide; a interface avisa) |

## Plataformas

| Plataforma | Situação | Como o ChatToon lê | O que você precisa |
|---|---|---|---|
| **YouTube** | ✅ funcional | chat público da live | o link da live ou o `@canal` (acompanha toda live nova) |
| **Twitch** | ✅ funcional | EventSub oficial com a sua conta, ou modo visitante sem login | nada no modo visitante; para o oficial, um app "Public" grátis em dev.twitch.tv e **Conectar conta** |
| **Kick** | ⚠️ limitada | **webhooks oficiais** | o seu app do Kick (Client ID + secret) e um endereço HTTPS público. O socket público não oficial foi desativado pelo Kick em outubro de 2026 |
| **TikTok** | 🧪 experimental | biblioteca não oficial TikTokLive + serviço Euler Stream (terceiro) | `chattoon extras install tiktok`; a live precisa estar no ar |
| **Facebook** | ⚠️ limitada | Graph API oficial | uma **Página** e um token do seu app da Meta |
| **Instagram** | ⚠️ limitada | Graph API oficial | conta **profissional** ligada a uma Página e token do seu app da Meta |
| **X** | ⚠️ limitada | não existe API para o chat de lives nem de Spaces; lê **respostas ao post** da live | o seu Bearer token da API do X (paga por post lido) |

Verificado em 17/10/2026, sem login: YouTube e Twitch (modo visitante) recebendo mensagens ao
mesmo tempo, sem duplicatas. Tokens ficam no **chaveiro do sistema** e nunca em perfis ou logs.
Detalhes e referências oficiais em [docs/PLATFORMS.md](docs/PLATFORMS.md).

## Configurações principais

| Onde | O quê |
|---|---|
| **Painel** | prévia ao vivo, estados, controles (iniciar, pausar, interromper, silenciar, emergência), conversar com o personagem, **Saúde do sistema** |
| **Personagens** | prévia interativa, expressões, gestos, curiosidades, animações em repouso, **reduzir movimento** |
| **Inteligência artificial** | Ollama, modelos, sugestões; no modo avançado, geração e qualidade das respostas |
| **Voz** | voz de cada personagem, velocidade, tom, mecanismos, leitura das mensagens (nome, nome + mensagem), saída de áudio |
| **Plataformas de streaming** | fontes, contas, como a atenção é dividida entre as plataformas |
| **Chat ao vivo** | todas as mensagens, responder agora, pular, memória dos espectadores |
| **Configurações** | modo avançado, **Modo Privacidade para Lives**, tema, idioma, falas espontâneas, perfis, restaurar padrões por seção, diagnóstico |

Modos: **Demonstração** (sem plataforma), **Assistido** (você aprova) e **Automático**.
Atalhos: `Ctrl+Espaço` iniciar/pausar · `Ctrl+.` interromper · `Ctrl+M` silenciar ·
`Ctrl+Shift+X` ou `Pause` emergência · `Ctrl+1…9` páginas.

**Idiomas:** a interface segue o idioma do sistema (inglês ou português do Brasil; mude em
*Configurações → Idioma da interface*). O personagem fala o idioma escolhido em *Inteligência
artificial → Idioma das respostas*.

![Painel em inglês, tema claro](docs/screenshots/dashboard-english.webp)

## Solução de problemas

| Problema | O que fazer |
|---|---|
| Ollama "Não instalado" ou "Parado" | **Inteligência artificial → Instalar Ollama** ou **Iniciar Ollama**; ative *Iniciar o Ollama com o computador* |
| "Meus modelos não aparecem" | o painel avisa quando há **outro Ollama** neste computador (Alpaca, um terminal) — clique em **Usar este**; modelos de embedding aparecem em "não respondem ao chat" com o motivo |
| "Ainda não está pronto" ao Iniciar | o relatório diz o que falta e tem um botão para resolver; itens em amarelo são opcionais |
| Voz "Precisa de voz" | **Voz → Mecanismos → Baixar voz** (ou instale outro mecanismo) |
| OBS não mostra nada | o ChatToon precisa estar aberto; confira o endereço (**Copiar** em *Onde mostrar*) |
| OBS de outro computador | rede de casa: libere o firewall pelo aviso do ChatToon; internet: **Testar a conexão** no assistente |
| Voz duas vezes | não acontece por projeto; se ouvir eco, o OBS captura o *Áudio do desktop* **e** a fonte |
| Respostas lentas | use um modelo menor ou com aceleração da placa de vídeo; diminua *Contexto da live* |
| Fonte "Aguardando a live" | o canal não está ao vivo; o ChatToon conecta quando a live começar |
| Kick "mudou o socket público" | use o modo oficial com webhooks (*Plataformas de streaming → Contas*) |
| Personagem do desktop não fica acima | fora do KDE Plasma no Wayland, use o "sempre visível" do seu gerenciador de janelas |

Diagnóstico completo: `chattoon doctor` ou *Configurações → Diagnóstico*.

## Segurança e privacidade

- **Nada do chat é executado** e quem está no chat **não reprograma** o personagem: as
  mensagens são dados, nunca instruções.
- O overlay escuta **só neste computador**, atrás de um código secreto no endereço. Rede de casa
  e internet só se você ativar; o Ollama e os controles do app nunca ficam expostos.
- Mudanças no sistema (instalar pacotes, iniciar serviços, firewall) só depois de uma
  confirmação que mostra o que será feito, com a senha pedida pelo próprio sistema.
- Logins sem senha, no chaveiro do sistema; segredos nunca vão para logs, perfis ou o overlay.
- **Modo Privacidade para Lives** ligado por padrão; o IP público só é consultado se você
  pedir e aparece oculto.
- Sem telemetria. Detalhes em [docs/SECURITY.md](docs/SECURITY.md).

## Desenvolvimento

```bash
python -m pytest -q              # cerca de 550 testes
python -m pytest -q --run-live   # também Ollama, TTS BigLinux e vozes Kokoro reais
python tools/update_translations.py
```

[Arquitetura](docs/ARCHITECTURE.md) · [Plataformas](docs/PLATFORMS.md) ·
[Respostas, memória, emoções e voz](docs/INTELLIGENCE.md) · [Segurança](docs/SECURITY.md) ·
[Desenvolvimento, testes e empacotamento](docs/DEVELOPMENT.md) · [Histórico de versões](CHANGELOG.md)

**Contribua:** use, teste, abra [issues](https://github.com/ruscher/ChatToon/issues) e envie
melhorias por pull request. Antes de enviar: `ruff check chattoon tests`, `python -m pytest -q`
e, se mudou textos da interface, `python tools/update_translations.py`.

## A história

Quem acompanha o **Pedro Bezerra** no canal **InfoRecarga** conhece a cena: live de Linux e
tecnologia, chat correndo mais rápido do que dá para ler e um apresentador só para dar conta de
tudo. A ideia do Pedro foi dar à live um **coapresentador** — um bichinho 3D com personalidade
que lê o chat, responde com bom humor e fala em voz alta.

O primeiro protótipo, a "versão 1", juntava scripts em Python e Node.js, uma cobra 3D que
virou lagarta (a avó da Minhoca), IA primeiro na nuvem e depois local com o Ollama, e as
regras de ouro do canal — respostas curtas, humor leve e nada de política ou religião. Ele
continua guardado no [histórico do projeto](https://github.com/ruscher/ChatToon/tree/fdb7a4f/legacy).

O **Rafael Ruscher**, também voluntário do BigLinux, viu o potencial: um *cartoon* do *chat*,
o **ChatToon**. O aplicativo nasceu na **2.0.0**, reconstruído do zero sobre as peças que o
BigLinux já tinha — a IA local e o TTS BigLinux —, ganhou novos personagens e a leitura das
mensagens na 2.1, várias plataformas ao mesmo tempo na 2.2, respostas corretas e memória na
2.3 e, na **3.0**, a janela na área de trabalho, a instalação de tudo pela interface e o
cuidado com a privacidade de quem transmite. Veja o [histórico de versões](CHANGELOG.md).

## Autoria, licença e créditos

- **Autor e criador da ideia:** Pedro Bezerra, do canal [InfoRecarga](https://www.youtube.com/@pedro_bezerra).
- **Colaborador:** Rafael Ruscher, que batizou o projeto e o transformou em aplicativo.
- Feito por voluntários do projeto [BigLinux](https://www.biglinux.com.br).

O ChatToon é software livre, licenciado sob a **GPL-3.0-or-later** ([LICENSE](LICENSE)).

- [Three.js](https://threejs.org) 0.186.1 — MIT, incluído em `chattoon/web/vendor/three`.
- Ícones da interface no estilo [Lucide](https://lucide.dev) — ISC.
- O pinguim é um modelo procedural original inspirado no **Tux**, criado por Larry Ewing
  (lewing@isc.tamu.edu) com o GIMP.
- Vozes e tratamento de texto pelo [TTS BigLinux](https://github.com/biglinux/tts-biglinux);
  IA pelo [Ollama](https://ollama.com).
