# Patch de desempenho TTS — 8 de setembro de 2026

Implementação sobre `repo-20260906-195842.zip`. Este pacote contém somente arquivos novos ou alterados e pode ser aplicado na raiz pelo atualizador normal do bot. Não é uma cópia completa do projeto.

SHA-256 da base: `c40e506009da74cf073c9c0fc826d98b9031f6b8390a529f5a5abb1fd396c354`.

## O que foi implementado

- Edge e gTTS compartilham uma única síntese para pedidos simultâneos com o mesmo texto e configurações. Reprodução, antecipação da próxima fala e envio de arquivo mantêm consumidores independentes; cancelar um deles não interrompe os demais.
- Prioridade acompanha a síntese quando uma fala antecipada passa a ser a fala atual. Semáforos mantêm limites físicos de concorrência, inclusive quando uma requisição gTTS bloqueante ainda está encerrando após timeout ou cancelamento.
- Streaming usa memória limitada, com transbordamento para arquivo temporário, cursores independentes e espera por disponibilidade de escrita no FIFO. O FFmpeg pode iniciar enquanto o serviço ainda sintetiza, limitado a dois decodificadores antecipados por padrão.
- gTTS reutiliza sessões HTTP por thread, verifica cancelamento entre partes e tentativas e aplica limites nativos de conexão/leitura. A sessão é renovada por ociosidade, idade ou número de requisições. O padrão de concorrência passa a dois, respeitando configurações explícitas existentes.
- A entrada elimina entregas repetidas da mesma mensagem antes do processamento. Filas aceitam ou recusam o conjunto inteiro de partes, verificam duplicatas antes de descartar falas e usam uma geração nova ao limpar a fila. Divisão de texto respeita o limite de bytes UTF-8 escapados do Edge, inclusive as reticências de truncamento.
- Configurações da fala são capturadas uma vez. As chaves de cache distinguem texto, maiúsculas, pontuação, voz, idioma, domínio gTTS, velocidade, tom e motor efetivamente usado no fallback. Catálogo de vozes mantém a última cópia válida e atualiza em segundo plano com timeout.
- Publicação de cache é atômica. Leases de arquivos e verificação de inode preservam áudio em uso e não removem uma substituição recente. Dois cancelamentos consecutivos também aguardam o término físico de gravações antes de liberar seus recursos.
- O worker oferece streaming HTTP progressivo v2 autenticado e upload binário de cache com checksum. A resposta só termina com sucesso quando todo o áudio foi recebido. Se falhar antes de enviar áudio, o bot pode sintetizar localmente; áudio parcial não vira cache nem aciona repetição dentro do mesmo stream.
- Workers anteriores continuam atendidos pelas rotas existentes de arquivo completo. A escolha de streaming exige capacidade anunciada. Cache local existente pode atender reprodução remota sem outra síntese. Limites de admissão do worker são atômicos e a fila do provedor é limitada.
- Roteamento usa amostras recentes por motor e tamanho do texto, separando primeiro áudio, tempo total, cache e fallback. Uma fração limitada dos pedidos reais permite comparar rotas; a admissão de uma fala não inicia sondagens de rede.
- O music agent recebe áudio progressivo, mantém buffer PCM limitado e cancela apenas a fala indicada por servidor e ID do pedido. Cancelamentos que chegam antes do pedido também são reconhecidos. A remoção de um overlay TTS preserva música e outros overlays.
- Métricas distinguem espera por vaga, primeiro áudio, síntese, cache e primeiro frame observado. Tempos enviados pelo worker são durações do worker, sem comparar relógios de máquinas diferentes.
- Cache Opus preparado foi implementado como opção: prepara em segundo plano frases repetidas, conserva frames completos e fornece um cursor por reprodução. Limites de memória, quantidade, duração e TTL evitam crescimento contínuo.
- Manutenção de cache é agrupada em segundo plano; percentis e varreduras evitam trabalho repetido. A limpeza externa usa uma varredura, protege arquivos bloqueados ou recentes e mantém orçamentos separados para Piper. Formatação de mensagens de debug fica condicionada ao modo de debug.

## Instalação e compatibilidade

O patch atualiza o phone worker para **1.11.6** e o music agent para **0.3.28**. O módulo `tts_transport.py` está nas listas canônicas de instalação, sincronização e atualização. Scripts verificam as versões de `edge-tts==7.2.8` e `gTTS==2.5.4`, já usadas na VPS, e respeitam as políticas existentes de instalação automática.

A atualização completa pelo painel agora reutiliza o manifesto e o pacote do atualizador automático. Isso permite ao bootstrap já instalado receber o novo módulo, sem tentar enviá-lo à lista de arquivos de um atualizador inline antigo. O reparo apenas de scripts continua sem solicitar reinício. Instalações que já careciam do bootstrap persistente continuam dependendo do procedimento de reparo existente no projeto.

Aplicar o ZIP pelo fluxo usual do bot e deixar o fluxo existente atualizar o worker. As identidades do worker Termux e do filho APK, os códigos e a compilação do aplicativo Android não foram alterados. Não há `.env`, credenciais, dados, logs ou pacotes instalados neste ZIP.

## Configurações e limites

As melhorias principais entram com os padrões do patch; valores explícitos no ambiente continuam prevalecendo.

| Configuração | Padrão | Efeito |
| --- | --- | --- |
| `TTS_GTTS_CONCURRENCY` | `2` | Limite de sínteses gTTS na VPS. |
| `TTS_GTTS_SESSION_MAX_REQUESTS` | `256` | Renova a sessão após esse número de requisições. |
| `TTS_STREAM_MEMORY_BUDGET_BYTES` | `16777216` | Orçamento do replay comprimido em RAM, com transbordamento para disco; buffers de transporte têm limites próprios. |
| `TTS_FFMPEG_OVERLAP_ENABLED` | `true` | Início antecipado do decodificador no caminho elegível de streaming. |
| `TTS_FFMPEG_OVERLAP_CONCURRENCY` | `2` | Número máximo de decodificadores antecipados. |
| `TTS_PREPARED_OPUS_CACHE_ENABLED` | `false` | Ativar com `true` para preparar frases repetidas; permanece opcional por consumir CPU. |
| `TTS_PREPARED_OPUS_CACHE_MAX_BYTES` | `8388608` | Limite do cache Opus preparado quando ativado. |
| `PHONE_WORKER_TTS_AGENT_CONCURRENCY` | `2` | Admissões simultâneas no sintetizador do worker. |

A preparação Opus tem ainda limites internos de 128 entradas, TTL de 600 segundos e 512 KiB/40 segundos por frase. O replay comprimido limita a memória por pedido antes de usar disco e aplica o teto de áudio configurado, de 8 MiB por padrão. Limpeza preserva arquivos em uso e arquivos muito recentes; por isso as cotas de disco são metas de manutenção, não um limite instantâneo rígido durante reprodução.

## Validação executada

- **125 testes TTS passaram**, contra 94 da base. Incluem deduplicação, admissão de grupos, cancelamento compartilhado, cancelamento remoto por pedido, limpeza de fila, cache e roteamento.
- FFmpeg real: processo iniciado antes da chegada dos bytes; primeiro frame PCM produzido antes do fim da síntese; uma única instância utilizada; processos encerrados ao final.
- HTTP real em localhost, com provedores simulados: resposta progressiva, autenticação, resposta interrompida sem EOF de sucesso, cache binário com checksum e preservação de cache anterior em upload inválido.
- Concorrência e recursos: liberação de vagas só após término físico, duas solicitações de cancelamento durante escrita, proteção de arquivos bloqueados e substituição de inode.
- Pacote de distribuição do worker validado pelo bootstrap da base original: 23 membros aceitos. O teste do painel verifica instalação do novo módulo e igualdade do hash instalado com o hash publicado.
- Todos os 23 arquivos Python alterados compilaram; os 6 scripts shell passaram por `bash -n`.
- O ZIP final passou pelo verificador de segurança original do atualizador, com conferência de caminhos, integridade e igualdade byte a byte com os arquivos alterados.

O ambiente de validação não dispõe de pytest nem das dependências normais de Discord/Edge/gTTS. A suíte unittest usa os stubs já existentes no projeto e Requests fornecido pelo pip; os testes de HTTP, sistema de arquivos e FFmpeg citados acima usam recursos reais. A suíte geral baseada em pytest e reprodução em Discord/provedores externos não foram executadas.

Para repetir a suíte TTS em um ambiente com as dependências do projeto:

```bash
python -m unittest discover -s tests -p 'test_tts*.py'
```

Não foi feita implantação nem benchmark de produção. O ganho de 10× não foi medido nem garantido. Após aplicar, comparar primeiro frame, tempo total, p50/p95, falhas e acertos de cache em textos curtos e longos, por motor e rota, com cache frio e quente separados.

## Arquivos do patch

- `cleanup-audio-temp.sh`
- `cogs/tts/aliases.py`
- `cogs/tts/audio.py`
- `cogs/tts/cog.py`
- `cogs/tts/helpers.py`
- `cogs/tts/prefix.py`
- `cogs/tts/prepared.py`
- `cogs/tts/routing.py`
- `cogs/tts/runtime.py`
- `cogs/tts/streaming.py`
- `cogs/tts/utils/message_dispatch.py`
- `cogs/tts/utils/message_gate.py`
- `config.py`
- `deploy/termux/phone-worker/bootstrap-phone-worker.sh`
- `deploy/termux/phone-worker/install.sh`
- `deploy/termux/phone-worker/music_agent.py`
- `deploy/termux/phone-worker/phone_worker.py`
- `deploy/termux/phone-worker/phone_worker_bootstrap.py`
- `deploy/termux/phone-worker/start-phone-music-agent.sh`
- `deploy/termux/phone-worker/start-phone-worker.sh`
- `deploy/termux/phone-worker/tts_transport.py`
- `music_system/audio_router.py`
- `scripts/core-worker-automation.py`
- `scripts/sync-phone-worker.sh`
- `tests/test_core_worker_cleanup_and_apk_self_build.py`
- `tests/test_tts_helpers.py`
- `tests/test_tts_runtime.py`
- `tests/test_tts_worker_streaming.py`
- `utility/commands/workers.py`

O próprio relatório, `docs/tts-performance-patch.md`, completa os 30 arquivos do pacote.
