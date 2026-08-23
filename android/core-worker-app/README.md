# Core Worker 0.7.6 — Gradle 9 com javaagent seguro

A versão `0.7.6` usa o Termux somente como **builder bootstrap**, mantendo Termux e APK como runtimes separados durante a transição. O primeiro APK dessa transição é compilado e assinado pelo `phone_worker.py` no celular, com a mesma keystore das versões já instaladas. A VPS não executa Gradle, Android SDK, NDK ou `aapt2`: ela prepara o ZIP de fontes, entrega segredos temporários por job autenticado e publica o APK pronto recebido do celular.

O bundle do toolchain agora usa manifesto v6. Além de `executablePaths`, ele normaliza o `DEFAULT_JVM_OPTS` do launcher Gradle para o `/system/bin/sh`, impedindo que o Java interprete `"-Xmx64m"` como classe principal. No Gradle 9, o `-javaagent:$APP_HOME/...` é preservado somente depois que o JAR é validado dentro da distribuição copiada; qualquer outra expansão de shell continua bloqueada. O APK rejeita bundles antigos sem essas garantias. A publicação lê `packageName`, `versionName` e `versionCode` diretamente do `AndroidManifest.xml` compilado; metadados divergentes ou downgrade preservam o APK e o `latest.json` anteriores.

Durante o bootstrap, o Termux preserva o worker canônico e a porta `8766`; o APK usa o runtime filho `<worker-id>-apk` e a porta `8767`. Isso impede que heartbeats Android sobrescrevam a versão/capabilities do `phone_worker.py` e garante que `worker_update` e o primeiro `apk_build_debug` continuem chegando ao Termux. Pareamentos novos criados diretamente pelo APK permanecem dedicados e usam a porta `8766`.

Durante esse primeiro build, o próprio `phone_worker.py` monta automaticamente um bundle privado com o JDK, Gradle, Android SDK 34, `aapt2` compatível com Termux e **somente as bibliotecas Bionic encontradas pela árvore transitiva `DT_NEEDED`**. O worker não copia mais todo o `$PREFIX/lib`, não inclui LLVM/FFmpeg/Python sem necessidade e reprova o bundle se Java, Gradle ou `aapt2` falharem no smoke isolado. A árvore ELF é lida com `readelf`/`llvm-readelf` quando disponível e possui parser ELF64 interno como fallback, portanto o bootstrap não passa a depender de outro pacote do Termux. Esse bundle entra no APK bootstrap. Depois da instalação, o APK extrai e retém o toolchain no armazenamento interno, executa smoke tests reais de Java, Gradle e `aapt2` e somente então anuncia:

- role/capability `apk-builder`;
- job `apk_build_debug`;
- job `apk_publish_last`;
- estado `apk_self_builder.ready=true` no heartbeat.

A partir desse momento, os jobs de build preferem o APK. O Termux continua disponível como fallback controlado até que um build completo feito pelo próprio APK seja homologado. Se o bundle estiver ausente, incompleto ou não executar no aparelho, o APK não anuncia capacidade de build e o job permanece no Termux, sem falso positivo.

## Interface e estado do build

O painel do Discord agora mostra um único estado por pipeline, diferencia fila ativa de falha encerrada e exibe o detalhe do último erro sem repetir duas mensagens de APK. O aplicativo concentra conexão, runtime e autobuild no primeiro card, mantém diagnóstico e manutenção recolhidos e apresenta o preflight do builder com estados visuais distintos: verificando, pronto, republicação disponível ou bloqueado.

A automação reconcilia `core_worker_automation_pending.json` com o estado real do job no registry. Um job `failed`, `expired` ou `cancelled` deixa de aparecer como “build pendente”; retry automático respeita cooldown e retry manual continua disponível após a correção.

## Compatibilidade de execução do self-builder

Esta distribuição privada mantém `compileSdk 34`, `minSdk 26` e **`targetSdk 28`** intencionalmente. Aplicações destinadas à API 29 ou superior não podem executar binários extraídos no diretório gravável do próprio app em Android 10+. Como o self-builder precisa executar o toolchain privado criado no bootstrap, o gate de build impede publicar essa variante com `targetSdk` acima de 28.

O APK não é destinado ao Google Play. A capacidade só é anunciada depois de executar no próprio aparelho:

```text
java -version
gradle --version
aapt2 version
```

Se o Android ou o fabricante bloquear qualquer etapa, o APK permanece sem a capability `apk-builder` e o Termux continua responsável pelo build.

## Fluxo de build

```text
Primeira transição
VPS prepara fonte/segredos → Termux gera o bundle mínimo e compila/assina → VPS valida a identidade compilada e publica → APK 0.7.6 é instalado

Atualizações seguintes
VPS prepara fonte/segredos → APK compila/assina com o bundle retido → VPS publica → APK atualiza
```

A base de código pode omitir o arquivo pesado:

```text
app/src/main/assets/core-linux/android-builder/android-builder-toolchain.zip
```

No bootstrap, o Termux gera esse ZIP automaticamente com o layout mínimo:

```text
manifest.json
jdk/bin/java
jdk/bin/javac
jdk/bin/jar
gradle/bin/gradle
android-sdk/platforms/android-34/android.jar
bin/aapt2
runtime-libs/*.so*
```

O manifesto usa o schema `core-worker-android-builder-v1`, versão 6, arquitetura ARM64, runtime `termux-bionic-direct`, registra a estratégia `dt-needed-transitive-v1` e o launcher `android-sh-resolved-app-home-jvm-opts-v2`. O Gradle valida o ZIP antes de empacotá-lo. O APK executa uma validação mais forte após extrair o bundle e não depende do rootfs de Bedrock, PRoot ou de Python externo para buildar.

O APK continua substituindo o Termux no runtime normal: serviço foreground autônomo, fila autenticada `/core-worker/jobs/poll`, resultados em `/core-worker/jobs/result`, API compatível na porta `8767` durante o bootstrap compartilhado (e `8766` em pareamentos APK dedicados), TTS nativo e tarefas allowlist. Não existe shell remoto livre nem comando arbitrário. Bedrock continua condicionado aos binários e preflights próprios.

Configuração de transição:

```text
CORE_WORKER_APK_REPLACES_TERMUX=true
CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED=true
CORE_WORKER_APK_SELF_BUILDER_PREFERENCE_SECONDS=90
CORE_WORKER_APK_UPLOAD_MAX_BYTES=1073741824
PHONE_WORKER_APK_BUILD_SOURCE_MAX_BYTES=1073741824
PHONE_WORKER_APK_SELF_BUILDER_REBUILD=false
```

Após homologar um autobuild completo, `CORE_WORKER_TERMUX_BOOTSTRAP_BUILDER_ENABLED=false` desativa apenas a manutenção automática do bootstrap. O registry continua preferindo o APK validado e ainda pode usar o Termux manualmente como recuperação enquanto ele estiver registrado.

> As seções abaixo registram patches antigos e podem citar fluxos anteriores. O comportamento atual é o descrito nesta seção 0.7.6.

## Patch 86: Core Linux Runtime v1 sem Termux

A versão `0.5.55` transforma o Core Linux interno em uma etapa validável de ponta a ponta sem Termux: o APK agora anuncia `supported_tasks` reais para a VPS, prepara/valida um rootfs scaffold controlado em `files/core-linux/rootfs/` e executa um smoke test seguro combinando executor JNI allowlist + rootfs + estado persistente.

O escopo continua protegido: não baixa rootfs externo, não abre shell livre, não chama Python/Chaquopy para logs leves, não inicia Bedrock e não toca Termux. Jobs antigos de Linux/Bedrock/Python pesado seguem ocultos ou bloqueados até o runtime real ficar pronto.

## Patch 85.7: terminal interno e logs ao vivo

A versão `0.5.50` transforma o terminal da aba Bedrock em um console técnico do Core Worker: ele agora guarda histórico em `files/core-linux/logs/bedrock-terminal.log`, acompanha eventos/status em tempo real, permite copiar logs direto pelo app e abre uma tela de terminal expandida com botão **Mínimo** para voltar.

O terminal continua seguro: não é shell livre do Android. Ele aceita comandos controlados como `help`, `status`, `logs`, `test`, `prepare`, `clear` e `copy`, enquanto comandos reais do Bedrock seguem bloqueados até rootfs/runtime serem validados sem crash.

## Patch 85.5: modo seguro real para Bedrock/rootfs

A versão `0.5.48` corta a origem mais provável do crash pós-**Testar servidor**: o APK não anuncia nem executa jobs automáticos de rootfs, Bedrock ou Python pesado enquanto essa etapa ainda não estiver validável. Se a VPS ainda tiver algum job antigo desses na fila, o app responde como **pausado por segurança**, em vez de tocar Chaquopy/rootfs/Bedrock em segundo plano.

O botão **Testar servidor** agora roda em uma thread dedicada, com cooldown curto, logs explícitos em `files/core-linux/logs/app-startup.log`, sem usar o fluxo genérico de busy global e sem disparar probes profundos. Diagnósticos de bundle/relatório também foram rebaixados para snapshots leves, usando apenas arquivos locais e estado salvo. O switch do Bedrock fica bloqueado enquanto o runtime não estiver realmente pronto, evitando start acidental do serviço antes da validação.

## Patch 85.4: Bedrock/rootfs sem travar a interface

A versão `0.5.47` deixa o botão **Testar servidor** totalmente leve e local: ele não chama Python/Chaquopy, não toca JNI, não inicia serviço, não baixa nada e não tenta executar Bedrock. O teste agora apenas valida arquivos/estados locais de rootfs, executor, Box64, EULA e Bedrock, grava `files/core-linux/logs/bedrock-test.log` e `files/core-linux/logs/rootfs-check.log`, e retorna uma mensagem natural quando o runtime ainda não está pronto.

Também evita alerta falso de **Permissões necessárias** na abertura: o card só aparece depois de uma pendência estável em verificações consecutivas, e some imediatamente quando todas as permissões estão ok. A preparação do Core Linux na abertura foi reduzida ao esqueleto leve; probes profundos de rootfs/Python ficam para ação explícita ou jobs permitidos.

## Patch 85.3: permissões reais e teste Bedrock protegido

A versão `0.5.46` corrige dois pontos da etapa Rootfs/Bedrock: o card **Permissões necessárias** não nasce mais visível por padrão e só aparece depois da verificação real quando falta notificação, instalação de APK ou liberação de bateria; a tela principal continua acessível mesmo se alguma permissão estiver pendente. Também remove o retângulo vazio do card quando tudo está ok.

O botão **Testar servidor** agora executa o diagnóstico Bedrock em fluxo protegido: impede toque duplicado, usa timeouts por etapa, serializa o Python embutido para evitar corrida com outros jobs, captura erro parcial e sempre devolve resumo seguro em vez de travar/crashar a interface.

## Patch 85.1: abertura segura da MainActivity

A versão `0.5.44` adiciona um guard de inicialização para impedir tela branca depois da entrada do Rootfs Manager. A MainActivity agora desenha uma tela segura imediatamente, adia rootfs/Python/probes para depois da primeira renderização, registra falhas em `files/core-linux/logs/app-startup.log` e mostra modo seguro com detalhe caso a UI principal falhe. O rootfs continua assistido e validável; Box64/Bedrock real ainda ficam para patches futuros.

# Patch 85 — rootfs interno assistido

A versão `0.5.43` adiciona o primeiro Rootfs Manager interno do APK. O app cria e valida um scaffold controlado em `files/core-linux/rootfs/`, com estado persistente em `runtime/rootfs-state.json`, manifesto local, staging separado e logs próprios.

Escopo seguro desta etapa:

- prepara/valida rootfs interno dentro do armazenamento app-specific do Android;
- não baixa rootfs externo, Box64 ou Bedrock;
- não abre shell livre e não aceita comando arbitrário da VPS;
- mantém Termux somente como fallback legado;
- deixa Bedrock bloqueado por Box64/bedrock_server/EULA, mas não mais por falta da estrutura rootfs quando o scaffold for validado.

# Core Worker APK privado

# Patch 59 — status interno completo do APK

O Patch 59 aprofunda a migração iniciada no Patch 58. O APK continua em modo híbrido, mas agora envia status interno mais completo diretamente para a VPS, sem depender do Termux para essa telemetria.

O heartbeat direto passa a incluir:

- versão e código do APK;
- perfil aplicado;
- estado do FCM/push;
- bateria e temperatura;
- tipo de rede, VPN e ping TCP leve para a VPS quando possível;
- estado de atualização;
- último erro conhecido do runtime interno;
- separação explícita entre APK interno e Termux worker.

O painel `workers` passa a mostrar o APK interno separado do Termux worker. O Termux continua responsável por jobs reais, build e shell nesta etapa. O APK apenas prova que já consegue manter telemetria própria com a VPS.

Também corrige o aviso do painel Discord `WorkersPanelView._refresh was never awaited`, evitando conflito com método interno privado do `discord.ui.LayoutView`.

A VPS continua sem build Android pesado. O phone worker continua responsável por compilar e assinar APKs.

# Patch 57 — assinatura compatível pelo phone worker

O Patch 57 corrige o conflito de pacote do Android ao atualizar por cima:

- o APK continua sendo buildado no phone worker, não na VPS Oracle;
- a VPS não usa `gradle`, Android SDK nem `apksigner`;
- a keystore antiga compatível fica local em `/home/ubuntu/secrets/core-worker-upload.keystore`;
- a VPS envia essa keystore somente pelo payload autenticado do job para o phone worker;
- o phone worker grava a keystore apenas no workspace temporário de build;
- o APK gerado sai assinado com a mesma chave da versão já instalada;
- a keystore não entra no GitHub, ZIP público, logs ou painel;
- se a keystore faltar, o job falha com mensagem clara e não publica APK incompatível.

Arquivos locais esperados:

```text
/home/ubuntu/secrets/core-worker-upload.keystore
android/core-worker-app/app/google-services.json
/home/ubuntu/secrets/firebase-service-account.json
```

A service account do Firebase continua somente na VPS e nunca vai para o phone worker.

## Patch 56 — APK builder sem segredos públicos e sem assinatura na VPS

O Patch 56 corrige o fluxo de build automático com Firebase:

- `google-services.json` continua fora do GitHub e fora do ZIP público de source.
- A VPS lê o `google-services.json` local e envia o conteúdo somente no payload autenticado do job para o phone worker builder.
- O phone worker grava esse arquivo apenas no workspace temporário de build.
- O ZIP `source-core-worker-app.zip` não deve conter `google-services.json`, `.env`, keystore, service account, `local.properties` ou configs privadas.
- O phone worker compila e gera o APK debug já assinado pelo Gradle.
- A VPS Oracle não usa `gradle`, `apksigner`, Android SDK nem keystore no fluxo normal; ela só valida o ZIP/APK, publica `latest.json` e envia FCM.
- Se a build falhar, a automação registra falha recente e evita repetir em loop.

Arquivos locais esperados na VPS/build env:

```text
android/core-worker-app/app/google-services.json
/home/ubuntu/secrets/firebase-service-account.json
```

Nenhum desses arquivos vai para GitHub. A service account também nunca vai para o phone worker.

## v0.5.15 — preparação do runtime interno

A versão `0.5.15` complementa o Patch 55. Ela começa a reduzir a dependência futura do Termux sem remover nem quebrar o fluxo atual.

O comportamento desta versão é intencionalmente conservador:

- o **Termux phone-worker continua sendo o worker oficial** e executa todos os jobs reais;
- o APK cria e mantém um espaço privado `core-runtime/` apenas em modo preview;
- a telemetria passa a informar `runtime_mode=termux` e o estado do runtime interno;
- o painel `workers` mostra se o celular está em **Termux atual** ou em runtime interno futuro;
- nenhuma função pesada foi migrada ainda;
- build Android pesado continua sendo responsabilidade do phone worker builder, não da VPS Oracle.

Objetivo da etapa:

```text
Termux atual funcionando -> APK prepara runtime interno -> migrar primeiro health ping pequeno -> só depois migrar jobs reais
```

O runtime interno ainda **não substitui** Termux, Termux:API, Termux:Boot ou Tailscale. Ele apenas prepara a estrutura para que isso aconteça em patches futuros, com fallback seguro.

## v0.5.14 — polimento visual e tela principal mais leve

A versão `0.5.14` complementa o Patch 54.

Principais ajustes desta versão:

- tela inicial com textos mais curtos e visual mais limpo;
- somente a ação principal continua em botão azul forte; ações secundárias ficaram mais discretas;
- perfil, conexão, atualizações e detalhes técnicos ocupam menos espaço;
- detalhes técnicos continuam recolhidos por padrão;
- não muda a arquitetura FCM: push real continua em modo seguro com fallback local.


Correções principais:

- o perfil do app agora usa uma fonte única de verdade e normaliza `profile`/`profile_label`, evitando aparecer `Normal`/`Mídia` em um bloco e `Turbo` em outro;
- escolher um perfil não finge que aplicou: o app só atualiza o perfil persistido quando o usuário toca em **Aplicar perfil**;
- depois de aplicar, a lista de perfis fecha e todos os blocos usam o mesmo perfil;
- a tela principal ficou mais compacta, sem numeração e com textos mais curtos;
- detalhes de Termux, rede, SSHD, jobs, FCM e versões continuam recolhidos em **Detalhes técnicos**;
- ações perigosas, como esquecer conexão local, ficam visualmente separadas;
- a VPS continua só orquestrando/publicando; build Android pesado deve ser feito pelo phone worker com função `apk-builder`/`turbo`.

Arquivos locais necessários continuam fora do Git:

```text
/home/ubuntu/secrets/firebase-service-account.json
android/core-worker-app/app/google-services.json
```

## v0.5.12 — FCM seguro em camadas

A versão `0.5.12` complementa o Patch 52. O FCM volta a ser ativado, mas seguindo um fluxo mais seguro:

- o build valida o `app/google-services.json` local antes de gerar APK;
- o `google-services` Gradle plugin volta a processar a configuração do Firebase;
- o app abre primeiro e só depois tenta registrar o token FCM;
- se Firebase, Google Play Services, permissão de notificação ou VPS falhar, a tela principal continua funcionando;
- o botão **Verificar agora** não depende de FCM;
- o `FirebaseMessagingService` fica mínimo: recebe data push, mostra notificação quando permitido, agenda checagem local e reporta best-effort;
- `JobScheduler` continua como fallback local.

Arquivos Firebase continuam locais no ambiente de build/phone worker e fora do Git:

```text
/home/ubuntu/secrets/firebase-service-account.json
android/core-worker-app/app/google-services.json
```


Este diretório contém o APK privado **Core Worker**.

O APK está evoluindo para o objetivo final:

```text
instalou o APK -> preparou o celular -> pareou -> virou worker da VPS
```

Hoje ele ainda é um **companion de onboarding**: guia Termux, Termux:API, Termux:Boot e Tailscale, fala com o phone-worker local em `127.0.0.1` e conecta o worker real à VPS. O controle pesado continua no Discord/VPS pelo painel `workers`.

## v0.5.10 — hotfix FCM/notification crash guard

A versão `0.5.10` complementa o Patch 51b. Ela corrige o crash loop observado na `0.5.9` quando o APK reportava eventos de notificação/FCM e a VPS respondia erro 500.

Correções principais:

- `webserver.py` passa a gravar `data/core_worker_app_notifications.json` com tmp único e lock, evitando corrida entre vários reports simultâneos do APK;
- `/core-worker/app/notification` não derruba mais a VPS por falha de escrita local;
- o APK protege inicialização, registro de token FCM, checagem de update e reports com crash guard;
- se Firebase/Google Play Services/backend falhar, o app mostra estado de push indisponível/temporário e continua abrindo;
- `JobScheduler` continua como fallback local.

## v0.5.9 — FCM push real + fallback local

A versão `0.5.9` complementa o Patch 51. O APK passa a registrar o token FCM da instalação na VPS e a VPS pode enviar push real quando publicar uma nova versão do APK. O push é enviado por FCM HTTP v1 usando a service account local da VPS, mantida fora do Git.

Arquivos locais necessários para build/envio:

```text
/home/ubuntu/secrets/firebase-service-account.json
  -> credencial privada da VPS para enviar FCM
  -> nunca vai para GitHub

android/core-worker-app/app/google-services.json
  -> config Android do Firebase usada pelo Gradle
  -> fica local no ambiente de build
  -> ignorado pelo Git neste projeto
```

Fluxo novo:

```text
APK abre ou recebe novo token FCM
  -> POST /core-worker/app/fcm-token
  -> VPS salva token em data/core_worker_app_fcm_tokens.json com chmod 600

Worker builder publica APK novo
  -> webserver.py atualiza releases/latest.json
  -> VPS envia mensagem FCM data-only de alta prioridade
  -> CoreWorkerFirebaseMessagingService recebe mesmo com app fechado, quando o Android permitir
  -> APK mostra notificação e reporta fcm_received/fcm_displayed
```

O `JobScheduler` periódico continua como fallback. FCM reduz o atraso, mas Android ainda pode bloquear entrega se o app for forçado a parar, se Google Play Services estiver indisponível ou se a permissão de notificação estiver negada.

## v0.5.8 — modo usuário comum + painel workers limpo

A versão `0.5.8` complementa o Patch 50. A tela principal do APK fica mais parecida com um app normal: estado do celular, conexão com a VPS principal, perfil resumido e atualização. Perfil detalhado, Termux, rede privada, jobs, SSHD, portas e botões de sincronização ficam recolhidos em áreas técnicas ou em ações explícitas.

O painel `workers` do Discord também passa a priorizar leitura humana: mostra quantos celulares estão online, se há atualização pendente e o resumo do worker selecionado. Registry, roles, PID, SSHD, portas, duplicatas do Termux e caminhos locais deixam de aparecer no card principal e ficam para **Detalhes do celular**/diagnóstico.

Estados de update agora usam linguagem mais clara:

- `APK: instalado X` quando o app aberto realmente reportar a versão publicada;
- `APK: instalação pendente` quando a notificação/download chegou, mas o Android ainda não concluiu a instalação;
- `Worker: atualização pendente` apenas se algum worker online estiver abaixo da versão esperada.

## v0.5.7 — UX limpa + checagem local com app fechado

A versão `0.5.7` complementa o Patch 49. A tela principal foi simplificada para usuário comum: o app mostra se o celular está pronto, se está conectado à VPS principal e se existe atualização. Termux, Termux:API, Termux:Boot, Tailscale, SSHD, jobs, portas, versões e outras informações de diagnóstico ficam recolhidos em **Detalhes técnicos**.

Quando o worker local já está pareado, a etapa **Conectar à VPS** vira **Conectado à VPS principal** e o campo de código fica escondido. O formulário só volta a aparecer ao tocar em **Trocar/refazer pareamento**.

A checagem de atualização com app fechado usa `JobScheduler` periódico do Android. Isso é uma notificação local/best-effort, não push instantâneo: sem FCM, sem serviço foreground permanente e sem o usuário abrir o app pelo menos uma vez, o Android pode atrasar a execução. Quando o job roda, ele consulta `/core-worker/app/latest.json`, compara `versionCode`, evita notificação duplicada e reporta estados como `background_displayed`, `background_duplicate` ou `background_permission_missing` para a VPS.

O painel `workers` também diferencia:

- notificação entregue/exibida, mas APK ainda antigo: **atualização pendente de instalação**;
- instalador aberto, mas app ainda antigo: **instalação pendente**;
- app aberto já na versão publicada: **app atualizado**.

## v0.5.6 — botão Atualizar com feedback real

A versão `0.5.6` complementa o Patch 48. O botão **Atualizar** não deve mais falhar em silêncio: ao tocar, o app mostra status no banner superior, registra o clique para a VPS, mostra progresso do download direto do APK, valida SHA-256 quando o `latest.json` informar hash e tenta abrir o instalador Android usando o arquivo local. Se o instalador local falhar, o fallback abre a URL direta do arquivo `.apk`, não uma página intermediária. Eventos de clique, download, validação e abertura do instalador não são deduplicados, para que cada tentativa apareça no diagnóstico da VPS.

## v0.5.4 — estado local do agent e telemetria de pareamento

A versão `0.5.4` complementa o Patch 46: ao abrir/voltar para o app, o APK verifica o worker local e reporta à VPS se encontrou o agent já pareado, se ele está offline ou se ainda não está vinculado. A tela também mostra o resumo do canal SSH/SSHD informado pelo agent local, ajudando a entender por que o botão remoto de wake pode falhar mesmo com Termux/Tailscale abertos.

## v0.5.3 — pareamento automático, notificação e download direto

A versão `0.5.3` mantém IP/porta reais fora do código versionado e melhora a comunicação APK ⇄ VPS ⇄ phone-worker. Ao abrir, o app verifica automaticamente se o Termux worker local já está pareado e salva esse estado sem exigir novo código. O app passa a consumir `downloadUrl`/`directApkUrl`, reportar `download_started`, `download_verified` e abertura do instalador para a VPS, e o botão Atualizar baixa o APK direto da VPS e abre o instalador local, sem mandar para página intermediária.

Build privado do APK:

```text
A VPS Oracle de 1 GB RAM não deve compilar o APK.
Ela só orquestra, valida arquivos leves, enfileira o job e publica o resultado.
O build pesado deve rodar automaticamente em um phone worker com perfil `builder` ou `turbo`.
```

Antes de acionar o build pelo worker, valide apenas os arquivos locais leves na VPS:

```bash
cd /home/ubuntu/bot
python3 -m json.tool android/core-worker-app/app/google-services.json >/dev/null && echo "google-services.json OK"
grep -n '"package_name"' android/core-worker-app/app/google-services.json
ls -l /home/ubuntu/secrets/firebase-service-account.json
```

Se a URL não for injetada pelo fluxo privado de build, o app mostra **VPS não configurada no build** e bloqueia pareamento/update em vez de expor IP real no repositório.

## v0.4.6 — permissões obrigatórias e aviso automático de update

A versão `0.4.6` adiciona a tela inicial de permissões do Core Worker. A tela principal só é liberada quando o app tiver o necessário para funcionar como companion privado:

- notificações para avisar quando a VPS publicar um APK novo;
- permissão de instalar atualizações/APKs baixados da VPS;
- permissão/ajuste de bateria para reduzir a chance do Android matar o app em segundo plano.

Quando a VPS publicar `latest.json` com `notifyUsers`/`notificationRequested`, o app mostra o banner **Atualizar** no topo e tenta disparar uma notificação local. A URL da VPS continua fixa/read-only e não existe campo normal para escolher IP ou porta.

O objetivo continua o mesmo: hoje o app guia Termux/Termux:API/Tailscale; no futuro, essas dependências serão reduzidas ou embutidas aos poucos.

## v0.4.5 — updater robusto e worker builder sem sujeira local

A versão `0.4.5` mantém a URL da VPS fixa na tela normal, reforça o banner único de atualização no topo e documenta que builds feitos por worker devem ser publicados/assinados pela VPS sem deixar artefatos locais no repositório.

A VPS só publica/sinaliza que existe uma versão nova, e o APK cuida da experiência humana:

- o APK consulta sempre a VPS privada injetada no build e seu `/core-worker/app/latest.json`;
- se houver versão nova, mostra um aviso no topo com botão **Atualizar**;
- quando possível, envia uma notificação local de atualização;
- ao tocar em **Atualizar**, o APK baixa o arquivo indicado no manifesto, valida SHA-256 quando informado e abre o instalador do Android;
- se não houver update, o topo fica limpo e não mostra botão extra.

A interface principal fica em blocos mais naturais:

- **Este celular**;
- **Conectar à VPS** ou **Conectado à VPS principal**;
- **Perfil deste celular**;
- **Atualizações**;
- **Detalhes técnicos** recolhidos.

O pareamento continua correto:

```text
APK -> /local/pair -> Termux phone-worker -> /core-worker/pair na VPS
APK -> /local/heartbeat -> Termux phone-worker -> /core-worker/heartbeat na VPS
```

O APK não cria worker próprio, não salva token de worker e não envia heartbeat direto. No Android comum, a instalação ainda precisa de confirmação do usuário.

## Escopo correto do APK agora

O APK pode:

- detectar se Termux, Termux:API, Termux:Boot e Tailscale estão instalados;
- detectar o worker local em `http://127.0.0.1:8766/local/status`;
- testar conexão com a VPS privada configurada no build, sem o usuário digitar IP/porta;
- parear este celular usando o código `CORE-XXXX` gerado no painel `workers`;
- passar o pareamento para o phone-worker real via `POST /local/pair`;
- editar o **perfil deste próprio celular** por `POST /local/profile`;
- pedir heartbeat/status básico ao worker local por `POST /local/heartbeat`;
- procurar atualização privada do APK na VPS;
- mostrar o botão **Atualizar** no topo apenas quando houver update;
- baixar o APK e abrir o instalador do Android pelo próprio app;
- abrir Termux/Tailscale quando o Android permitir.

O APK não deve virar, por enquanto:

- painel para gerenciar todos os workers;
- tela de fila completa de jobs;
- controle de failover;
- gerenciador de logs grandes;
- substituto do painel Discord;
- runtime completo que substitui o Termux;
- VPN embutida ainda.

## Relação com Termux, plugins e rede privada

Estado atual:

```text
Core Worker APK
  -> guia/prepara
  -> fala com o phone-worker local

Termux + phone_worker.py
  -> executa jobs reais
  -> heartbeat
  -> fila segura
  -> scripts/ffmpeg/diagnóstico

Termux:API
  -> bateria/recursos do Android para o worker

Termux:Boot
  -> boot automático do worker

Tailscale externo
  -> rede privada atual entre celular e VPS
```

Direção futura:

```text
Core Worker APK
  -> onboarding
  -> worker cada vez mais embutido
  -> plugins/recursos cada vez menos manuais
  -> rede privada própria estilo VPN/WireGuard/userspace
```

A VPS/Discord continua sendo o cérebro/orquestrador.

## Rotas locais esperadas no phone-worker

```text
GET  http://127.0.0.1:8766/local/status
POST http://127.0.0.1:8766/local/profile
POST http://127.0.0.1:8766/local/pair
POST http://127.0.0.1:8766/local/heartbeat
```

Essas rotas devem aceitar apenas localhost. Elas não expõem shell livre, token global, fila completa, controle pesado ou ações perigosas.

## Perfis disponíveis

- `leve`: diagnósticos e logs;
- `midia`: logs, ZIP, FFmpeg, FFprobe e TTS/cache;
- `completo`: mídia + manutenção;
- `bedrock`: perfil futuro para Minecraft Bedrock, sem assumir Java.

O APK altera apenas o perfil do celular onde ele está instalado.

## Fluxo de uso

1. Abra o app **Core Worker**.
2. Em **Estado deste celular**, toque em **Verificar agora**.
3. Se faltar algo, abra **Detalhes técnicos** para ver Termux, Termux:API, Termux:Boot e Tailscale.
4. No Discord, abra o painel `workers`.
5. Vá em **Adicionar celular → Gerar código**.
6. No APK, preencha apenas:
   - código `CORE-XXXX`;
   - nome do celular.

   A URL da VPS é fixa no app e não aparece como escolha normal para o usuário.
7. Toque em **Testar conexão**.
8. Toque em **Conectar este celular à VPS**.
9. Se precisar, toque em **Alterar perfil**, escolha o perfil e toque em **Aplicar perfil**.
10. No Discord, toque em **Atualizar**.

> Use HTTP apenas dentro da rede privada Tailscale/rede privada equivalente. Se a VPS ficar exposta publicamente, use HTTPS antes de parear.

## Atualização privada pela VPS

O APK procura atualização aqui:

```text
GET /core-worker/app/latest.json
```

E baixa o APK pelo `apkUrl` indicado no manifesto quando o usuário toca em **Atualizar** no topo do app. Com o app fechado, o job local periódico também consulta esse manifesto quando o Android permitir.

### Endpoint na VPS

`webserver.py` serve arquivos a partir de:

```text
CORE_WORKER_APK_DIR
```

Se a variável não existir, o padrão é:

```text
/home/ubuntu/bot/android/core-worker-app/releases
```

considerando que o bot rode a partir de `/home/ubuntu/bot`.

### Publicar uma versão nova

O caminho normal é:

```text
VPS detecta mudança / usuário aciona build
  -> painel workers enfileira job apk_build_debug
  -> phone worker builder baixa a base
  -> phone worker compila o APK
  -> worker envia o APK para POST /core-worker/app/publish
  -> VPS publica e atualiza latest.json
```

A VPS não deve executar `gradle`, `assembleDebug` ou Android build localmente. Em Oracle 1 GB RAM, ela deve ficar como cérebro/orquestradora.

Para checar o estado sem compilar:

```bash
cd /home/ubuntu/bot
python3 -m json.tool android/core-worker-app/app/google-services.json >/dev/null && echo "google-services.json OK"
python3 scripts/core-worker-automation.py status
```

## Build pelo phone worker

Use o painel `workers` ou a automação do Core Worker para enfileirar o job **Buildar APK**. O worker precisa declarar `apk-builder`, normalmente via perfil `Builder` ou `Turbo`.

## Build local fora da VPS, se necessário

1. Abra `android/core-worker-app` no Android Studio em uma máquina/celular capaz de compilar.
2. Aguarde o Gradle sincronizar.
3. Selecione **Build > Build APK(s)**.
4. Instale o APK apenas nos seus celulares.

## Segurança

- Não coloque token da VPS, token Discord ou segredo no código Android.
- O APK não salva token de worker; o token real fica no `~/.phone-worker.env` do Termux.
- Não suba keystore privado para GitHub.
- O APK é privado para uso nos seus celulares.
- Atualização pela VPS deve ser usada pela rede privada.
- Sempre valide SHA-256 no `latest.json`.
- As rotas locais do phone-worker devem aceitar apenas `127.0.0.1`/localhost.

## Futuro planejado

Fases esperadas:

1. APK companion com Termux/Tailscale existentes.
2. APK guiando e automatizando cada vez mais setup, validação, atualização e recuperação.
3. APK reduzindo dependência manual de Termux/plugins.
4. Futuro grande: rede privada embutida/própria estilo VPN, provavelmente WireGuard/userspace ou equivalente.

Mesmo no futuro, a VPS/Discord deve continuar como cérebro/orquestrador e segredos devem continuar fora do GitHub.


## APK compilado por worker builder

Para evitar travar a VPS Oracle de 1 GB RAM, o painel `workers` pode enviar o job **Buildar APK** para um celular com perfil/função `builder` (`apk-builder`).

O fluxo é:

```text
VPS empacota android/core-worker-app em source-core-worker-app.zip
worker builder baixa o source
phone worker compila o APK debug já assinado pelo Gradle
phone worker envia APK + sha256 para a VPS
VPS valida/publica o APK e atualiza latest.json com o SHA-256 final
Core Worker APK mostra Atualizar no topo quando houver versão nova
```

A VPS continua só como orquestradora/publicadora. O build pesado fica no worker.

No Android comum, a instalação ainda exige confirmação do usuário.


## Assinatura fixa ao publicar APK de worker builder

Quando um worker builder compila o APK debug, o Gradle já assina o artefato no próprio phone worker. A VPS não deve depender de Android SDK/apksigner no fluxo normal: ela recebe o APK pronto, valida o ZIP/APK e publica. Assinatura fixa pela VPS fica opcional para um futuro release build, via variáveis locais, e não deve ser usada no Oracle VPS de 1 GB.

Configuração recomendada na VPS agora: **não definir** `CORE_WORKER_APK_SIGNING_MODE` ou deixar `disabled`. Assim a VPS aceita o APK debug já assinado pelo phone worker e não precisa de Android SDK.

Para produção privada futura, use assinatura fixa em outro ambiente adequado ou como etapa separada, com keystore local fora do Git. Exemplo apenas futuro:

```env
CORE_WORKER_APK_SIGNING_MODE=release
CORE_WORKER_APK_KEYSTORE=/home/ubuntu/secrets/core-worker-release.jks
CORE_WORKER_APK_KEY_ALIAS=core-worker
CORE_WORKER_APK_KEYSTORE_PASSWORD=...
CORE_WORKER_APK_KEY_PASSWORD=...
```

Nunca envie keystore, senhas, `.env`, `google-services.json` ou service account para GitHub. No fluxo atual, o APK publicado no `latest.json` é o APK debug já assinado pelo phone worker. A service account fica só na VPS.

## Perfil Turbo / ajudar VPS

O app agora oferece o perfil **Turbo**, pensado para um celular forte e confiável ajudar a VPS quando estiver disponível. Ele habilita capacidades como resumo de logs, auditoria de ZIP, testes de endpoint, mídia/FFmpeg e build de APK.

A VPS não depende desse perfil para funcionar. Se o celular estiver offline, o bot continua com fallback local.



## Patch 58 — runtime interno com heartbeat direto

- O APK passa para `0.5.17` / `versionCode 32`.
- O runtime interno deixa de ser apenas preview e envia um heartbeat leve direto para a VPS em `/core-worker/app/heartbeat`.
- Esse heartbeat não executa jobs e não substitui o Termux ainda; ele apenas prova que o APK consegue aparecer para a VPS sem passar pelo phone-worker.
- Jobs reais, build APK, diagnósticos e tarefas pesadas continuam no Termux/phone-worker por enquanto.
- A VPS continua sem build Android local: ela só orquestra, injeta arquivos locais no payload temporário e publica o APK recebido do phone worker.
- `google-services.json`, service account e keystore continuam fora do GitHub e fora dos ZIPs públicos.

## Patch 60 — detalhes técnicos e jobs leves do APK

- O APK passa para `0.5.19` / `versionCode 34`.
- A tela de **Detalhes técnicos** foi reorganizada em blocos menores: App, Aparelho, Runtime, Termux worker e dependências atuais.
- O token FCM não é mais exibido na UI; o app mostra apenas `token registrado`.
- O runtime interno começa a consultar jobs leves em `/core-worker/app/jobs/fetch` e reportar resultado em `/core-worker/app/jobs/result`.
- Jobs leves suportados nesta etapa: `apk_ping`, `apk_status_refresh` e `apk_report_logs`.
- Esses jobs não executam shell, não mexem no Termux, não compilam APK e não recebem comandos arbitrários.
- Jobs reais e tarefas pesadas continuam no Termux/phone-worker por enquanto.

## Patch 61 — hotfix jobs leves + FCM token cleanup

- O APK passa para `0.5.21` / `versionCode 36`.
- Heartbeat/status interno do APK agora roda em thread de background, evitando `NetworkOnMainThreadException`.
- A tela de detalhes não faz ping TCP durante renderização; pings ficam em fluxos de background.
- Resultados bons de jobs leves limpam erro transitório antigo de `NetworkOnMainThreadException`.
- Se a VPS receber `UNREGISTERED`/HTTP 404 do FCM, o token é invalidado e o app força renovação segura do token.
- O painel principal não mostra mais JSON bruto de erro FCM nem último erro técnico do APK; detalhes ficam recolhidos.

## Patch 62 — jobs internos seguros do APK

- O APK passa para `0.5.21` / `versionCode 36`.
- O runtime interno executa jobs seguros em lista permitida, sem shell, sem Termux e sem build.
- Jobs suportados nesta etapa:
  - `apk_ping`;
  - `apk_status_refresh`;
  - `apk_report_logs`;
  - `apk_diagnostic`;
  - `apk_check_update`;
  - `apk_test_vps_connection`;
  - `apk_upload_report`;
  - `apk_clear_app_cache`;
  - `apk_sync_profile`;
  - `apk_download_small` limitado a 256 KiB e restrito à própria VPS.
- Jobs reais continuam no Termux/phone-worker.
- O APK não executa comandos arbitrários recebidos da VPS.
- A VPS continua só orquestrando; build Android pesado segue no phone worker.


## Patch 64 — migração interna avançada do APK

- O APK passa para `0.5.23` / `versionCode 38`.
- O runtime interno ganha diagnósticos mais úteis sem depender do Termux:
  - aparelho/bateria/permissões;
  - rede/VPN/ping para a VPS;
  - push/FCM/permissão de notificação;
  - atualização do APK;
  - runtime interno;
  - armazenamento/cache interno;
  - ponte APK interno ↔ Termux worker;
  - pacote completo de status do APK.
- Novos jobs internos seguros adicionados:
  - `apk_device_diagnostic`;
  - `apk_network_diagnostic`;
  - `apk_push_diagnostic`;
  - `apk_update_diagnostic`;
  - `apk_runtime_diagnostic`;
  - `apk_storage_diagnostic`;
  - `apk_worker_bridge_status`;
  - `apk_collect_status_bundle`;
  - `apk_cleanup_runtime_cache`.
- A VPS agenda diagnósticos internos automáticos com intervalos controlados e continua limitando tudo à allowlist.
- A UI de detalhes técnicos agora separa **Diagnósticos APK** de **Runtime**, mostrando resumo, armazenamento, ponte, último job e histórico sem jogar JSON bruto na tela principal.
- O painel `workers` passa a mostrar resumo de diagnóstico/armazenamento/ponte do APK interno junto do status do Termux worker.
- Ainda não há shell, Python interno, build pelo APK nem acesso ao diretório do Termux. Jobs reais continuam no phone-worker/Termux.

## Patch 63 — runtime interno avançado do APK

- O APK passa para `0.5.22` / `versionCode 37`.
- O runtime interno ganhou fila de jobs mais robusta, com estado `pending`, `running`, `ok`, `failed` e `timeout` salvo na VPS.
- A VPS agora rastreia jobs em execução, reentrega jobs expirados com retry limitado e registra timeout em vez de perder jobs silenciosamente.
- O APK mantém histórico local curto dos últimos jobs internos e faz deduplicação defensiva para não executar o mesmo job duas vezes.
- Novos jobs internos seguros:
  - `apk_upload_app_logs`;
  - `apk_sync_runtime_state`;
  - `apk_cache_cleanup`;
  - `apk_verify_file`;
  - `apk_job_history`.
- `apk_download_small` continua restrito à própria VPS e ao cache interno do app, com limite de tamanho e validação opcional de SHA-256.
- Transferências continuam usando armazenamento específico do app (`getCacheDir()`/diretório interno do APK), sem acessar Termux, armazenamento geral ou pastas externas.
- FCM continua mínimo: ele marca solicitação de acordar/sincronizar e agenda a checagem local; trabalho real continua fora de `onMessageReceived`.
- Ainda não há shell, Python interno, build Android pelo APK ou acesso a arquivos do Termux. Jobs reais continuam no phone-worker/Termux.

## Patch 65 — runtime interno maduro e painel de controle dos jobs APK

- O APK passa para `0.5.24` / `versionCode 39`.
- A VPS normaliza nomes antigos de jobs internos por alias para evitar duplicação falsa:
  - `apk_clear_app_cache` e `apk_cleanup_runtime_cache` viram `apk_cache_cleanup`;
  - `apk_report_logs` vira `apk_upload_app_logs`;
  - `apk_status_refresh` vira `apk_sync_runtime_state`.
- Jobs internos agora são classificados entre:
  - automáticos, que podem ser agendados periodicamente pela VPS;
  - manuais, que precisam de payload/ação explícita e não contam como “sem resultado”.
- A VPS agenda mais diagnósticos automáticos seguros do APK, incluindo aparelho, rede, push, update, runtime, armazenamento, ponte, histórico, pacote de status e limpeza controlada de cache.
- O arquivo `data/core_worker_app_jobs.json` passa a guardar catálogo e resumo por instalação, com cobertura dos jobs automáticos, manuais, pendentes e rodando.
- O painel `workers` ganhou a ação **Testar runtime APK**, que agenda todos os jobs automáticos seguros para o celular selecionado.
- O resumo de jobs no painel mostra cobertura real, por exemplo `jobs internos: 12/14 ok · aquecendo · 6 manuais`, em vez de tratar jobs manuais como falhas.
- A UI técnica do APK passa a mostrar a cobertura do catálogo de jobs recebido da VPS.
- Continua sem shell, sem Python interno, sem build pelo APK e sem acesso ao Termux. Jobs reais seguem no phone-worker/Termux.

## Versão 0.6.2 — agente autônomo de jobs

- O polling de `/core-worker/app/jobs/fetch`, a execução da allowlist e o envio para `/core-worker/app/jobs/result` saíram da `MainActivity`.
- `CoreWorkerRuntimeService` agora é o único proprietário da fila e continua trabalhando com a interface fechada.
- Resultados são gravados primeiro em uma outbox interna e só são removidos depois que a VPS confirma `ok: true`.
- O agente usa executor serial, deduplicação local, backoff exponencial, wake lock limitado e recuperação pelo boot, `JobScheduler` e FCM.
- A tela apenas solicita sincronização e lê o estado persistido; ela não executa jobs nem confirma resultados.
- O catálogo de 44 jobs seguros foi centralizado em `CoreWorkerJobCatalog`, evitando divergência entre Activity, serviço e heartbeat periódico.
- Um stop explícito grava `agent_enabled=false` e não é revertido automaticamente por boot, FCM ou reabertura do app. A reativação acontece pelo controle do runtime ou por novo pareamento.
- Esta etapa não libera shell arbitrário, Bedrock, Box64 real ou os jobs pesados ainda dependentes de binários/serviços externos.
