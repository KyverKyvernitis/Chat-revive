#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Aceitação real do pipeline Core Worker. Executar NO APARELHO/TERMUX.
# Não imprime token, conteúdo de keystore, google-services ou URLs privadas.

PROJECT_DIR=""
APK_PATH=""
INSTALL_APK=0
WAIT_SELF_BUILD_SECONDS=0
REPORT_PATH="${CORE_WORKER_ACCEPT_REPORT:-$HOME/core-worker-acceptance-report.json}"

usage() {
  cat <<'TXT'
Uso:
  accept-core-worker-on-device.sh --project /caminho/android/core-worker-app [opções]

Opções:
  --apk ARQUIVO                  valida um APK já produzido em vez de descobrir app-debug.apk
  --install                      instala/atualiza o APK com pm install -r
  --wait-self-build-seconds N    aguarda latest.json comprovar build publicado pelo runtime APK
  --report ARQUIVO               caminho do relatório sanitizado
TXT
}

while (($#)); do
  case "$1" in
    --project) PROJECT_DIR="${2:-}"; shift 2 ;;
    --apk) APK_PATH="${2:-}"; shift 2 ;;
    --install) INSTALL_APK=1; shift ;;
    --wait-self-build-seconds) WAIT_SELF_BUILD_SECONDS="${2:-0}"; shift 2 ;;
    --report) REPORT_PATH="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "argumento desconhecido: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$PROJECT_DIR" ]] || { echo "--project é obrigatório" >&2; exit 2; }
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd -P)"
[[ -f "$PROJECT_DIR/app/build.gradle" ]] || { echo "projeto Android inválido: $PROJECT_DIR" >&2; exit 3; }
[[ -x "$PROJECT_DIR/gradlew" || -f "$PROJECT_DIR/gradlew" ]] || { echo "gradlew ausente" >&2; exit 3; }
[[ -f "$PROJECT_DIR/gradle/wrapper/gradle-wrapper.jar" ]] || { echo "gradle-wrapper.jar ausente" >&2; exit 3; }

ENV_FILE="${PHONE_WORKER_ENV_FILE:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

TOKEN="${CORE_WORKER_TOKEN:-${PHONE_WORKER_TOKEN:-}}"
VPS_URL="${CORE_WORKER_SERVER_URL:-${PHONE_WORKER_VPS_URL:-${CORE_WORKER_VPS_URL:-}}}"
VPS_URL="${VPS_URL%/}"

TMP_DIR="$(mktemp -d "${TMPDIR:-$PREFIX/tmp}/core-worker-accept.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$(dirname "$REPORT_PATH")"

python_bin="$(command -v python || command -v python3 || true)"
[[ -n "$python_bin" ]] || { echo "python ausente" >&2; exit 4; }

json_state="$TMP_DIR/state.json"
printf '{}\n' > "$json_state"

put_json() {
  local key="$1" file="$2"
  KEY="$key" FILE="$file" STATE="$json_state" "$python_bin" - <<'PY'
import json, os
state_path=os.environ['STATE']; src=os.environ['FILE']; key=os.environ['KEY']
try: state=json.load(open(state_path, encoding='utf-8'))
except Exception: state={}
try: value=json.load(open(src, encoding='utf-8'))
except Exception as exc: value={"ok":False,"error":f"{type(exc).__name__}: {exc}"}
state[key]=value
tmp=state_path+'.tmp'
with open(tmp,'w',encoding='utf-8') as fh: json.dump(state,fh,ensure_ascii=False,indent=2,sort_keys=True)
os.replace(tmp,state_path)
PY
}

sanitize_text() {
  TOKEN_VALUE="$TOKEN" VPS_VALUE="$VPS_URL" "$python_bin" -c 'import os,sys; s=sys.stdin.read();
for v in (os.environ.get("TOKEN_VALUE",""), os.environ.get("VPS_VALUE","")):
    if v: s=s.replace(v,"<redacted>")
print(s[:12000], end="")'
}

collect_preflight() {
  OUT="$1" PROJECT="$PROJECT_DIR" "$python_bin" - <<'PY'
import json, os, shutil, subprocess, time
from pathlib import Path
p=Path(os.environ['PROJECT']); out={"ok":True,"termux": "com.termux" in os.environ.get("PREFIX","")}
def cmd(argv, timeout=20):
    try:
        r=subprocess.run(argv,cwd=p,capture_output=True,text=True,errors='replace',timeout=timeout)
        return {"rc":r.returncode,"output":((r.stdout or '')+(r.stderr or ''))[:1200]}
    except Exception as e: return {"rc":-1,"output":f"{type(e).__name__}: {e}"}
mem={}
try:
    for line in Path('/proc/meminfo').read_text().splitlines():
        if ':' in line:
            k,v=line.split(':',1); parts=v.split();
            if parts and parts[0].isdigit(): mem[k]=int(parts[0])*1024
except Exception: pass
out['memory']={"total":mem.get('MemTotal',0),"available":mem.get('MemAvailable',mem.get('MemFree',0))}
out['storage']={"free":shutil.disk_usage(p).free,"total":shutil.disk_usage(p).total}
out['tools']={}
for name, argv in {
 'java':['java','-version'],'javac':['javac','-version'],'jar':['jar','--version'],
 'gradle':['./gradlew','--version','--no-daemon'],'aapt2':['aapt2','version']}.items(): out['tools'][name]=cmd(argv,300 if name=='gradle' else 20)
try:
    r=subprocess.run(['termux-battery-status'],capture_output=True,text=True,timeout=3)
    b=json.loads(r.stdout) if r.returncode==0 and r.stdout.strip().startswith('{') else {}
    out['battery']={k:b.get(k) for k in ('percentage','status','plugged','temperature')}
except Exception: out['battery']={"available":False}
out['gradle_wrapper']={"jar":(p/'gradle/wrapper/gradle-wrapper.jar').is_file(),"properties":(p/'gradle/wrapper/gradle-wrapper.properties').read_text(errors='replace')[:800] if (p/'gradle/wrapper/gradle-wrapper.properties').is_file() else ''}
json.dump(out,open(os.environ['OUT'],'w'),ensure_ascii=False,indent=2,sort_keys=True)
PY
}

collect_preflight "$TMP_DIR/preflight.json"
put_json preflight "$TMP_DIR/preflight.json"

run_build() {
  local label="$1" clean_first="$2" out="$TMP_DIR/build-$label.json" log="$TMP_DIR/build-$label.log"
  local start end rc
  start="$(date +%s)"
  set +e
  (
    cd "$PROJECT_DIR"
    export GRADLE_USER_HOME="${CORE_WORKER_ACCEPT_GRADLE_HOME:-$HOME/.core-worker-accept-gradle}"
    mkdir -p "$GRADLE_USER_HOME"
    if [[ "$clean_first" == "1" ]]; then ./gradlew clean --no-daemon --max-workers=1 --console=plain; fi
    ./gradlew assembleDebug --no-daemon --max-workers=1 --stacktrace --console=plain
  ) >"$log" 2>&1
  rc=$?
  set -e
  end="$(date +%s)"
  RC="$rc" START="$start" END="$end" LOG="$log" LABEL="$label" "$python_bin" - <<'PY' > "$out"
import json,os
log=open(os.environ['LOG'],errors='replace').read()[-12000:]
print(json.dumps({"ok":int(os.environ['RC'])==0,"returncode":int(os.environ['RC']),"duration_seconds":int(os.environ['END'])-int(os.environ['START']),"log_tail":log},ensure_ascii=False,indent=2))
PY
  put_json "build_$label" "$out"
  return "$rc"
}

# Cold e warm são deliberadamente reais. Se o primeiro falhar, ainda geramos relatório.
set +e
run_build cold 1
cold_rc=$?
run_build warm 0
warm_rc=$?
set -e

if [[ -z "$APK_PATH" ]]; then
  APK_PATH="$(find "$PROJECT_DIR/app/build/outputs/apk/debug" -maxdepth 1 -type f -name '*.apk' -print 2>/dev/null | head -n 1 || true)"
fi

APK_PATH_VALUE="$APK_PATH" PROJECT="$PROJECT_DIR" "$python_bin" - <<'PY' > "$TMP_DIR/apk.json"
import hashlib,json,os,subprocess,zipfile
from pathlib import Path
p=Path(os.environ.get('APK_PATH_VALUE',''))
out={"ok":False,"path_present":bool(str(p)) and p.is_file()}
if p.is_file():
    raw=p.read_bytes(); out.update({"ok":True,"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()})
    try:
        with zipfile.ZipFile(p) as z:
            names=z.namelist()
        forbidden=[n for n in names if 'android-builder-toolchain.zip' in n or n.endswith('.cwpart') or '/jdk/' in n.lower() or '/android-sdk/' in n.lower()]
        out['forbidden_toolchain_assets']=forbidden[:20]; out['asset_gate_ok']=not forbidden
    except Exception as e: out['zip_error']=f"{type(e).__name__}: {e}"; out['ok']=False
    for tool,args,key in [('aapt',['aapt','dump','badging',str(p)],'identity'),('apksigner',['apksigner','verify','--verbose','--print-certs',str(p)],'certificate')]:
        try:
            r=subprocess.run(args,capture_output=True,text=True,errors='replace',timeout=30)
            out[key]={"rc":r.returncode,"output":((r.stdout or '')+(r.stderr or ''))[:3000]}
        except Exception as e: out[key]={"rc":-1,"output":f"{type(e).__name__}: {e}"}
print(json.dumps(out,ensure_ascii=False,indent=2))
PY
put_json apk "$TMP_DIR/apk.json"

if (( INSTALL_APK == 1 )) && [[ -n "$APK_PATH" && -f "$APK_PATH" ]]; then
  set +e
  install_out="$(pm install -r "$APK_PATH" 2>&1)"; install_rc=$?
  set -e
  INSTALL_RC="$install_rc" INSTALL_OUT="$install_out" "$python_bin" - <<'PY' > "$TMP_DIR/install.json"
import json,os
print(json.dumps({"ok":int(os.environ['INSTALL_RC'])==0,"returncode":int(os.environ['INSTALL_RC']),"output":os.environ['INSTALL_OUT'][:2000]},ensure_ascii=False,indent=2))
PY
  put_json install "$TMP_DIR/install.json"
fi

# Health do APK comprova download/smoke do toolchain sem acessar seu sandbox privado.
APK_HEALTH_PORT="${CORE_WORKER_APK_DIRECT_PORT:-8767}"
TOKEN_VALUE="$TOKEN" PORT_VALUE="$APK_HEALTH_PORT" "$python_bin" - <<'PY' > "$TMP_DIR/apk-health.json"
import json,os,urllib.request
out={"ok":False,"reachable":False}
token=os.environ.get('TOKEN_VALUE',''); port=os.environ.get('PORT_VALUE','8767')
if not token:
    out['error']='token ausente; health autenticado não consultado'
else:
    try:
        req=urllib.request.Request(f'http://127.0.0.1:{port}/health',headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=5) as r: data=json.loads(r.read(2_000_000))
        sb=data.get('apk_self_builder') if isinstance(data,dict) else {}
        out={"ok":bool(data.get('ok')),"reachable":True,"runtime_kind":data.get('runtime_kind'),"runtime_mode":data.get('runtime_mode'),"version":data.get('version'),"version_code":data.get('version_code'),"worker_id":data.get('worker_id'),"physical_worker_id":data.get('physical_worker_id'),"capabilities":data.get('capabilities'),"apk_self_builder":sb}
    except Exception as e: out['error']=f'{type(e).__name__}: {e}'
print(json.dumps(out,ensure_ascii=False,indent=2))
PY
put_json apk_health "$TMP_DIR/apk-health.json"

if (( WAIT_SELF_BUILD_SECONDS > 0 )); then
  deadline=$(( $(date +%s) + WAIT_SELF_BUILD_SECONDS ))
  success=0
  while (( $(date +%s) <= deadline )); do
    if [[ -n "$VPS_URL" ]]; then
      set +e
      latest="$($python_bin - "$VPS_URL" <<'PY'
import json,sys,urllib.request
url=sys.argv[1].rstrip('/')+'/core-worker/app/latest.json'
try:
    with urllib.request.urlopen(url,timeout=5) as r: print(r.read(1_000_000).decode('utf-8','replace'))
except Exception: print('{}')
PY
)"
      set -e
      SELF_LATEST="$latest" "$python_bin" - <<'PY' > "$TMP_DIR/self-build-check.json"
import json,os
try:d=json.loads(os.environ.get('SELF_LATEST') or '{}')
except Exception:d={}
runtime=str(d.get('builderRuntime') or d.get('builder_runtime') or '').lower()
worker=str(d.get('builderWorkerId') or d.get('builder_worker_id') or '').lower()
ok=('apk' in runtime) or worker.endswith('-apk')
print(json.dumps({"ok":ok,"builder_runtime":runtime,"builder_worker_id":worker,"versionName":d.get('versionName'),"versionCode":d.get('versionCode'),"sourceFingerprint":d.get('sourceFingerprint')},ensure_ascii=False,indent=2))
PY
      if "$python_bin" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("ok") else 1)' "$TMP_DIR/self-build-check.json"; then success=1; break; fi
    fi
    sleep 10
  done
  [[ -f "$TMP_DIR/self-build-check.json" ]] || printf '{"ok":false,"error":"latest.json não pôde ser consultado"}\n' > "$TMP_DIR/self-build-check.json"
  put_json first_apk_self_build "$TMP_DIR/self-build-check.json"
fi

# Resultado final: sem segredos/URLs privadas. O relatório não afirma homologação se faltou alguma prova.
COLD_RC="$cold_rc" WARM_RC="$warm_rc" STATE="$json_state" OUT="$REPORT_PATH" "$python_bin" - <<'PY'
import json,os,time
p=os.environ['STATE']; data=json.load(open(p,encoding='utf-8'))
apk=data.get('apk') or {}; health=data.get('apk_health') or {}; selfb=data.get('first_apk_self_build')
criteria={
 'cold_build': int(os.environ['COLD_RC'])==0,
 'warm_build': int(os.environ['WARM_RC'])==0,
 'apk_without_embedded_toolchain': bool(apk.get('asset_gate_ok')),
 'apk_identity_checked': bool((apk.get('identity') or {}).get('rc')==0),
 'apk_certificate_checked': bool((apk.get('certificate') or {}).get('rc')==0),
 'apk_runtime_health': bool(health.get('reachable') and health.get('runtime_kind')=='apk'),
 'apk_builder_ready_after_external_toolchain': bool((health.get('apk_self_builder') or {}).get('ready')),
}
if selfb is not None: criteria['first_self_build_published_by_apk']=bool(selfb.get('ok'))
data['criteria']=criteria
data['homologated']=all(criteria.values())
data['generated_at']=int(time.time())
data['report_schema']='core-worker-on-device-acceptance-v1'
with open(os.environ['OUT']+'.tmp','w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False,indent=2,sort_keys=True)
os.replace(os.environ['OUT']+'.tmp',os.environ['OUT'])
PY
chmod 600 "$REPORT_PATH" 2>/dev/null || true
printf 'Relatório: %s\n' "$REPORT_PATH"
"$python_bin" - "$REPORT_PATH" <<'PY'
import json,sys
x=json.load(open(sys.argv[1],encoding='utf-8'))
print('Homologado:', 'sim' if x.get('homologated') else 'não')
for k,v in (x.get('criteria') or {}).items(): print(f"- {k}: {'ok' if v else 'pendente/falhou'}")
PY
