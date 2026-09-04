package dev.core.worker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public class CoreWorkerRuntimeService extends Service {
    public static final String ACTION_START = "dev.core.worker.action.RUNTIME_START";
    public static final String ACTION_STOP = "dev.core.worker.action.RUNTIME_STOP";
    public static final String ACTION_TICK = "dev.core.worker.action.RUNTIME_TICK";
    public static final String ACTION_POLL_NOW = "dev.core.worker.action.AGENT_POLL_NOW";

    private static final String PREFS = "core_worker_private";
    private static final String CHANNEL_ID = "core_worker_runtime";
    private static final int NOTIFICATION_ID = 4107;
    private static final long TICK_MS = 30L * 1000L;
    private static final long HEARTBEAT_MIN_MS = 120L * 1000L;
    private static final long POLL_ERROR_BACKOFF_MIN_MS = 15L * 1000L;
    private static final long POLL_ERROR_BACKOFF_MAX_MS = 5L * 60L * 1000L;
    private static final long JOB_PROGRESS_INTERVAL_MS = 45L * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean running = false;
    private NativeTtsManager nativeTtsManager;
    private LocalNativeTtsHttpServer nativeTtsServer;
    private CoreWorkerDirectHttpServer directHttpServer;
    private final AtomicBoolean heartbeatRunning = new AtomicBoolean(false);
    private final AtomicBoolean pollRunning = new AtomicBoolean(false);
    private final ExecutorService agentExecutor = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "core-worker-agent");
        thread.setDaemon(true);
        return thread;
    });
    private CoreWorkerJobExecutor jobExecutor;
    private volatile long lastHeartbeatStartedAt = 0L;
    private volatile long nextPollAllowedAt = 0L;
    private volatile long pollErrorBackoffMs = POLL_ERROR_BACKOFF_MIN_MS;

    private final Runnable tickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            markTick("agente autônomo ativo");
            ensureDirectHttpServer();
            pollJobs("foreground_tick", false);
            reportHeartbeat("foreground_tick");
            handler.postDelayed(this, TICK_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        CoreWorkerRuntimeIdentity.migrate(getApplicationContext());
        createChannel();
        jobExecutor = new CoreWorkerJobExecutor(getApplicationContext());
        CoreWorkerApkBuildManager.refreshAsync(getApplicationContext());
        startNativeTtsBridge();
        ensureDirectHttpServer();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : String.valueOf(intent.getAction());
        String reason = intent == null ? "foreground_start" : intent.getStringExtra("reason");
        if (ACTION_STOP.equals(action)) {
            running = false;
            handler.removeCallbacks(tickRunnable);
            stopDirectHttpServer();
            prefs().edit()
                    .putBoolean("agent_enabled", false)
                    .putBoolean("job_executor_ready", false)
                    .putBoolean("foreground_runtime_active", false)
                    .putString("foreground_runtime_state", "agente autônomo parado")
                    .putLong("foreground_runtime_last_tick_at", System.currentTimeMillis())
                    .apply();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        running = true;
        startForeground(NOTIFICATION_ID, buildNotification("Agente autônomo ativo"));
        prefs().edit()
                .putBoolean("agent_enabled", true)
                .putBoolean("job_executor_ready", true)
                .putBoolean("foreground_runtime_active", true)
                .putString("foreground_runtime_state", "agente autônomo ativo")
                .putLong("foreground_runtime_started_at", System.currentTimeMillis())
                .apply();
        markTick("agente autônomo ativo");
        reportHeartbeat(reason);
        pollJobs(reason == null ? "foreground_start" : reason, ACTION_POLL_NOW.equals(action));
        handler.removeCallbacks(tickRunnable);
        handler.postDelayed(tickRunnable, TICK_MS);
        return START_STICKY;
    }

    static boolean shouldRunAgent(Context context) {
        if (context == null) return false;
        try {
            SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            if (prefs.contains("agent_enabled")) {
                return prefs.getBoolean("agent_enabled", false);
            }
            boolean paired = prefs.getBoolean("paired_via_native_apk", false)
                    && !prefs.getString("worker_token", "").trim().isEmpty();
            return prefs.getBoolean("foreground_runtime_active", false) || paired;
        } catch (Throwable ignored) {
            return false;
        }
    }

    public static void requestStart(Context context, String reason) {
        if (context == null) return;
        try {
            Intent intent = new Intent(context, CoreWorkerRuntimeService.class);
            intent.setAction(ACTION_START);
            intent.putExtra("reason", reason == null ? "request_start" : reason);
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
            else context.startService(intent);
        } catch (Throwable ignored) {
        }
    }

    public static void requestPoll(Context context, String reason) {
        if (context == null || !shouldRunAgent(context)) return;
        try {
            Intent intent = new Intent(context, CoreWorkerRuntimeService.class);
            intent.setAction(ACTION_POLL_NOW);
            intent.putExtra("reason", reason == null ? "request_poll" : reason);
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
            else context.startService(intent);
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        handler.removeCallbacks(tickRunnable);
        stopDirectHttpServer();
        stopNativeTtsBridge();
        try { agentExecutor.shutdownNow(); } catch (Throwable ignored) { }
        prefs().edit()
                .putBoolean("job_executor_ready", false)
                .putBoolean("foreground_runtime_active", false)
                .putString("foreground_runtime_state", "agente autônomo encerrado")
                .putLong("foreground_runtime_last_tick_at", System.currentTimeMillis())
                .apply();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }


    private void startNativeTtsBridge() {
        try {
            if (nativeTtsManager == null) {
                nativeTtsManager = new NativeTtsManager(getApplicationContext(), prefs());
                nativeTtsManager.warmUp();
            }
            if (nativeTtsServer == null) {
                nativeTtsServer = new LocalNativeTtsHttpServer(nativeTtsManager);
                nativeTtsServer.start();
            }
            prefs().edit()
                    .putBoolean("native_tts_bridge_active", true)
                    .putLong("native_tts_bridge_started_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable exc) {
            prefs().edit()
                    .putBoolean("native_tts_bridge_active", false)
                    .putString("native_tts_bridge_error", String.valueOf(exc.getMessage()))
                    .putLong("native_tts_bridge_started_at", System.currentTimeMillis())
                    .apply();
        }
    }

    private void stopNativeTtsBridge() {
        try {
            if (nativeTtsServer != null) {
                nativeTtsServer.stop();
                nativeTtsServer = null;
            }
        } catch (Throwable ignored) {
        }
        try {
            if (nativeTtsManager != null) {
                nativeTtsManager.shutdown();
                nativeTtsManager = null;
            }
        } catch (Throwable ignored) {
        }
        try {
            prefs().edit()
                    .putBoolean("native_tts_bridge_active", false)
                    .putLong("native_tts_bridge_stopped_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) {
        }
    }

    private void ensureDirectHttpServer() {
        try {
            if (nativeTtsManager == null) startNativeTtsBridge();
            if (directHttpServer != null && directHttpServer.isRunning()) return;
            directHttpServer = new CoreWorkerDirectHttpServer(getApplicationContext(), prefs(), nativeTtsManager);
            directHttpServer.start();
        } catch (Throwable error) {
            directHttpServer = null;
            prefs().edit().putBoolean("direct_http_active", false)
                    .putString("direct_http_error", "porta " + CoreWorkerRuntimeIdentity.effectiveDirectHttpPort(getApplicationContext()) + ": " + shortThrowable(error))
                    .putLong("direct_http_last_failure_at", System.currentTimeMillis()).apply();
        }
    }

    private void stopDirectHttpServer() {
        try {
            if (directHttpServer != null) directHttpServer.stop();
        } catch (Throwable ignored) {
        } finally {
            directHttpServer = null;
        }
    }

    private void markTick(String state) {
        try {
            prefs().edit()
                    .putBoolean("foreground_runtime_active", true)
                    .putString("foreground_runtime_state", state == null ? "serviço persistente ativo" : state)
                    .putLong("foreground_runtime_last_tick_at", System.currentTimeMillis())
                    .apply();
        } catch (Throwable ignored) {
        }
    }

    private void createChannel() {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (manager != null) {
                    NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "Runtime persistente Core Worker", NotificationManager.IMPORTANCE_LOW);
                    channel.setDescription("Mantém o agente do Core Worker ativo para buscar e executar jobs com a interface fechada.");
                    manager.createNotificationChannel(channel);
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pending = PendingIntent.getActivity(this, NOTIFICATION_ID, open, pendingFlags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentTitle("Core Worker autônomo")
                .setContentText(text == null ? "Runtime persistente ativo" : text)
                .setContentIntent(pending)
                .setOngoing(true)
                .setShowWhen(false);
        return builder.build();
    }

    private void pollJobs(String reason, boolean force) {
        if (!running || jobExecutor == null) return;
        long now = System.currentTimeMillis();
        if (!force && now < nextPollAllowedAt) return;
        if (!pollRunning.compareAndSet(false, true)) return;
        agentExecutor.execute(() -> {
            PowerManager.WakeLock wakeLock = null;
            try {
                PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                if (power != null) {
                    wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CoreWorker:AgentCycle");
                    wakeLock.setReferenceCounted(false);
                    wakeLock.acquire(10L * 60L * 1000L);
                }
                runAgentCycle(reason == null ? "background" : reason, force);
                pollErrorBackoffMs = POLL_ERROR_BACKOFF_MIN_MS;
            } catch (Throwable error) {
                long failedAt = System.currentTimeMillis();
                nextPollAllowedAt = failedAt + pollErrorBackoffMs;
                pollErrorBackoffMs = Math.min(POLL_ERROR_BACKOFF_MAX_MS, Math.max(POLL_ERROR_BACKOFF_MIN_MS, pollErrorBackoffMs * 2L));
                prefs().edit()
                        .putString("internal_light_jobs_state", "falha · " + shortThrowable(error))
                        .putString("internal_light_jobs_last_summary", "falha: " + shortThrowable(error))
                        .putString("internal_light_jobs_last_fetch_error", shortThrowable(error))
                        .putString("agent_last_error", shortThrowable(error))
                        .putLong("internal_light_jobs_last_check_at", failedAt)
                        .apply();
            } finally {
                try {
                    if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
                } catch (Throwable ignored) {
                }
                pollRunning.set(false);
            }
        });
    }

    private void runAgentCycle(String reason, boolean force) throws Exception {
        String serverUrl = normalizedServerUrl();
        String token = prefs().getString("worker_token", "").trim();
        String workerId = CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext());
        if (serverUrl.isEmpty() || token.isEmpty() || workerId.isEmpty()) {
            prefs().edit().putString("internal_light_jobs_state", "aguardando pareamento direto").apply();
            return;
        }

        flushResultOutbox(serverUrl);
        if (recoverInterruptedActiveJob(serverUrl, token)) {
            prefs().edit()
                    .putString("internal_light_jobs_state", "recuperando job interrompido")
                    .putString("internal_jobs_queue_summary", "aguardando reconciliação do job ativo")
                    .apply();
            return;
        }
        if (pendingResultOutboxCount() > 0) {
            prefs().edit()
                    .putString("internal_light_jobs_state", "resultado pendente")
                    .putString("internal_jobs_queue_summary", "resultado final aguardando confirmação")
                    .apply();
            return;
        }
        long startedAt = System.currentTimeMillis();
        JSONObject payload = buildForegroundHeartbeatPayload(reason);
        CoreWorkerRuntimeIdentity.putRuntimeFields(getApplicationContext(), payload);
        payload.put("force", force || shouldForcePoll(reason));
        payload.put("source", "core-worker-apk-agent-service-v2");

        prefs().edit()
                .putLong("internal_light_jobs_last_fetch_started_at", startedAt)
                .putString("internal_light_jobs_last_fetch_reason", reason)
                .putString("internal_light_jobs_last_fetch_app_version", BuildConfig.VERSION_NAME)
                .putInt("internal_light_jobs_last_fetch_app_version_code", BuildConfig.VERSION_CODE)
                .putString("internal_light_jobs_last_fetch_error", "")
                .apply();

        HttpResult response = request("POST", serverUrl + "/core-worker/jobs/poll", payload, token);
        long checkedAt = System.currentTimeMillis();
        SharedPreferences.Editor state = prefs().edit()
                .putLong("internal_light_jobs_last_check_at", checkedAt)
                .putInt("internal_light_jobs_last_fetch_http_status", response.status);
        if (!response.ok()) {
            String error = compact(response.body);
            state.putString("internal_light_jobs_state", "falha HTTP " + response.status)
                    .putString("internal_light_jobs_last_fetch_error", error)
                    .putString("agent_last_error", error).apply();
            throw new IllegalStateException("HTTP " + response.status + (error.isEmpty() ? "" : ": " + error));
        }

        JSONObject body = new JSONObject(response.body);
        if (!body.optBoolean("ok", false)) {
            String error = compact(body.optString("error", response.body));
            state.putString("internal_light_jobs_state", "VPS recusou a busca")
                    .putString("internal_light_jobs_last_fetch_error", error)
                    .putString("agent_last_error", error).apply();
            throw new IllegalStateException(error.isEmpty() ? "VPS recusou a busca de jobs" : error);
        }

        JSONObject remoteJob = body.optJSONObject("job");
        if (remoteJob == null || remoteJob.optString("job_id", "").trim().isEmpty()) {
            state.putInt("internal_light_jobs_last_count", 0)
                    .putInt("internal_light_jobs_last_returned_count", 0)
                    .putInt("internal_jobs_pending_count", 0)
                    .putInt("internal_jobs_running_count", 0)
                    .putString("internal_jobs_queue_summary", "fila autenticada vazia")
                    .putString("internal_jobs_catalog_summary", CoreWorkerJobCatalog.size() + " jobs APK · protocolo direto")
                    .putString("internal_light_jobs_state", "fila vazia")
                    .putString("internal_light_jobs_last_summary", "fila autenticada vazia")
                    .putString("agent_last_error", "").apply();
            return;
        }

        String jobId = remoteJob.optString("job_id", "").trim();
        String jobType = remoteJob.optString("type", "job").trim();
        JSONObject job = new JSONObject()
                .put("id", jobId)
                .put("job_id", jobId)
                .put("type", jobType)
                .put("attempt", remoteJob.optInt("attempts", 1))
                .put("payload", remoteJob.optJSONObject("payload") == null ? new JSONObject() : remoteJob.optJSONObject("payload"));
        persistActiveJob(job, "claimed", "job recebido da VPS");
        postJobProgress(serverUrl, token, jobId, "claimed", 1.0, "job persistido localmente antes da execução", "");
        state.putInt("internal_light_jobs_last_count", 1)
                .putInt("internal_light_jobs_last_returned_count", 1)
                .putInt("internal_jobs_running_count", 1)
                .putString("internal_jobs_queue_summary", "1 job autenticado em execução").apply();

        File existing = outboxFile(jobId);
        if (existing != null && existing.isFile()) {
            persistActiveJob(job, "result_pending", "resultado local já existe; reenviando à VPS");
            JSONObject pending = normalizeStoredEnvelope(readJsonFile(existing));
            if (postResultEnvelope(serverUrl, pending)) {
                rememberCompletedJob(jobId);
                existing.delete();
                clearActiveJob(jobId);
            }
            prefs().edit().putInt("internal_jobs_running_count", 0).apply();
            return;
        }

        JSONObject result;
        long jobStartedAt = System.currentTimeMillis();
        AtomicBoolean leaseKeeperRunning = new AtomicBoolean(true);
        Thread leaseKeeper = startJobLeaseKeeper(serverUrl, token, jobId, leaseKeeperRunning);
        try {
            if (wasJobRecentlyCompleted(jobId)) {
                result = new JSONObject().put("ok", true).put("type", jobType)
                        .put("deduplicated", true).put("message", "job duplicado ignorado pelo agente")
                        .put("jobId", jobId);
            } else {
                try {
                    String initialStage = CoreWorkerApkBuildManager.supports(jobType) ? "builder_preflight" : "executing";
                    persistActiveJob(job, initialStage, "execução iniciada");
                    postJobProgress(serverUrl, token, jobId, initialStage, 5.0,
                            CoreWorkerApkBuildManager.supports(jobType)
                                    ? "validando autobuilder antes de executar"
                                    : "executando job no APK", "");
                    if (CoreWorkerApkBuildManager.supports(jobType)) {
                        result = CoreWorkerApkBuildManager.execute(
                                getApplicationContext(), jobType, job.optJSONObject("payload"), serverUrl);
                    } else if (CoreWorkerJobCatalog.supports(jobType)) {
                        result = jobExecutor.execute(job, serverUrl);
                    } else if (CoreWorkerDirectTaskExecutor.supports(jobType)) {
                        JSONObject directPayload = new JSONObject(job.optJSONObject("payload").toString());
                        directPayload.put("task", jobType);
                        result = new CoreWorkerDirectTaskExecutor(getApplicationContext(), prefs(), nativeTtsManager).execute(directPayload);
                    } else {
                        result = new JSONObject().put("ok", false).put("type", jobType)
                                .put("error", "job não anunciado pelo APK").put("message", "job recusado pela allowlist");
                    }
                } catch (Throwable jobError) {
                    result = new JSONObject().put("ok", false).put("type", jobType)
                            .put("error", shortThrowable(jobError)).put("message", "job falhou no agente do APK");
                }
            }
        } finally {
            leaseKeeperRunning.set(false);
            if (leaseKeeper != null) leaseKeeper.interrupt();
        }
        result.put("durationMs", Math.max(0L, System.currentTimeMillis() - jobStartedAt));
        result.put("attempt", remoteJob.optInt("attempts", 1));
        JSONObject envelope = buildResultEnvelope(job, result);
        File stored = persistOutbox(jobId, envelope);
        persistActiveJob(job, "result_pending", "resultado final persistido; aguardando confirmação da VPS");
        postJobProgress(serverUrl, token, jobId, "result_pending", 99.0, "resultado final persistido localmente", "");
        boolean sent = postResultEnvelope(serverUrl, envelope);
        if (sent) {
            if (stored != null) stored.delete();
            rememberCompletedJob(jobId);
            clearActiveJob(jobId);
        }
        boolean ok = result.optBoolean("ok", false);
        String summary = compact(result.optString("message", result.optString("error", ok ? "concluído" : "falhou")));
        recordJobHistory(jobType, ok, summary);
        prefs().edit()
                .putString("internal_light_jobs_state", (ok ? "concluído" : "falhou") + (sent ? "" : " · resultado pendente"))
                .putString("internal_light_jobs_last_summary", jobType + " · " + summary)
                .putInt("internal_jobs_running_count", 0)
                .putString("internal_jobs_queue_summary", sent ? "resultado confirmado" : "resultado salvo na outbox")
                .putString("agent_last_error", sent ? "" : "resultado aguardando confirmação").apply();
    }

    private File activeJobFile() {
        File dir = new File(getFilesDir(), "apk-agent");
        if (!dir.exists()) dir.mkdirs();
        return new File(dir, "active-job.json");
    }

    private synchronized JSONObject readActiveJob() {
        try { return readJsonFile(activeJobFile()); }
        catch (Throwable ignored) { return new JSONObject(); }
    }

    private synchronized void persistActiveJob(JSONObject job, String stage, String summary) {
        try {
            String jobId = job == null ? "" : firstNonEmpty(job.optString("job_id", ""), job.optString("id", ""));
            if (jobId.isEmpty()) return;
            JSONObject previous = readActiveJob();
            long now = System.currentTimeMillis();
            long startedAt = jobId.equals(previous.optString("job_id", ""))
                    ? previous.optLong("started_at", now) : now;
            JSONObject value = new JSONObject()
                    .put("schema", "core-worker-apk-active-job-v1")
                    .put("job_id", jobId)
                    .put("type", job.optString("type", ""))
                    .put("attempt", job.optInt("attempt", 1))
                    .put("stage", stage == null ? "running" : stage)
                    .put("summary", summary == null ? "" : compact(summary))
                    .put("started_at", startedAt)
                    .put("updated_at", now)
                    .put("app_version", BuildConfig.VERSION_NAME)
                    .put("app_version_code", BuildConfig.VERSION_CODE);
            File target = activeJobFile();
            File temp = new File(target.getParentFile(), target.getName() + ".tmp");
            try (FileOutputStream output = new FileOutputStream(temp, false)) {
                output.write(value.toString().getBytes(StandardCharsets.UTF_8));
                output.flush();
            }
            if (!temp.renameTo(target)) {
                try (FileOutputStream output = new FileOutputStream(target, false)) {
                    output.write(value.toString().getBytes(StandardCharsets.UTF_8));
                    output.flush();
                }
                temp.delete();
            }
            prefs().edit()
                    .putString("active_job_id", jobId)
                    .putString("active_job_type", job.optString("type", ""))
                    .putString("active_job_stage", stage == null ? "running" : stage)
                    .putString("active_job_summary", summary == null ? "" : compact(summary))
                    .putLong("active_job_started_at", startedAt)
                    .putLong("active_job_updated_at", now)
                    .apply();
        } catch (Throwable error) {
            prefs().edit().putString("agent_last_error", "active-job: " + shortThrowable(error)).apply();
        }
    }

    private synchronized void clearActiveJob(String expectedJobId) {
        try {
            JSONObject active = readActiveJob();
            String current = active.optString("job_id", "").trim();
            if (expectedJobId != null && !expectedJobId.trim().isEmpty() && !current.isEmpty()
                    && !expectedJobId.trim().equals(current)) return;
            activeJobFile().delete();
            prefs().edit()
                    .remove("active_job_id")
                    .remove("active_job_type")
                    .remove("active_job_stage")
                    .remove("active_job_summary")
                    .remove("active_job_started_at")
                    .remove("active_job_updated_at")
                    .apply();
        } catch (Throwable ignored) { }
    }

    private int pendingResultOutboxCount() {
        File dir = jobExecutor == null ? null : jobExecutor.outboxDir();
        File[] files = dir == null ? null : dir.listFiles((d, name) -> name != null && name.endsWith(".json"));
        return files == null ? 0 : files.length;
    }

    private boolean recoverInterruptedActiveJob(String serverUrl, String token) {
        JSONObject active = readActiveJob();
        String jobId = active.optString("job_id", "").trim();
        if (jobId.isEmpty()) return false;
        String stage = active.optString("stage", "").trim();
        if ("result_pending".equals(stage)) {
            File outbox = outboxFile(jobId);
            if (outbox != null && outbox.isFile()) return true;
            clearActiveJob(jobId);
            return false;
        }
        boolean reconciled = postJobProgress(serverUrl, token, jobId, "executor_restarted", 0.0,
                "serviço APK reiniciou durante o job; solicitando requeue seguro", "abandon");
        if (reconciled) {
            clearActiveJob(jobId);
            prefs().edit()
                    .putString("internal_light_jobs_state", "job interrompido reconciliado")
                    .putString("internal_light_jobs_last_summary", jobId + " reencaminhado após restart")
                    .apply();
            return false;
        }
        return true;
    }

    private Thread startJobLeaseKeeper(
            String serverUrl, String token, String jobId, AtomicBoolean runningFlag) {
        Thread thread = new Thread(() -> {
            while (runningFlag.get()) {
                try { Thread.sleep(JOB_PROGRESS_INTERVAL_MS); }
                catch (InterruptedException interrupted) { break; }
                if (!runningFlag.get()) break;
                JSONObject active = readActiveJob();
                if (!jobId.equals(active.optString("job_id", ""))) break;
                String stage = active.optString("stage", "running");
                String summary = active.optString("summary", "job em execução no APK");
                postJobProgress(serverUrl, token, jobId, stage, -1.0, summary, "");
            }
        }, "core-worker-job-lease");
        thread.setDaemon(true);
        thread.start();
        return thread;
    }

    private boolean postJobProgress(
            String serverUrl, String token, String jobId, String stage, double progress, String summary, String action) {
        try {
            if (serverUrl == null || serverUrl.trim().isEmpty() || token == null || token.trim().isEmpty()
                    || jobId == null || jobId.trim().isEmpty()) return false;
            JSONObject payload = new JSONObject()
                    .put("worker_id", CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext()))
                    .put("job_id", jobId.trim())
                    .put("stage", stage == null ? "running" : stage)
                    .put("summary", summary == null ? "" : compact(summary));
            if (progress >= 0.0) payload.put("progress", Math.max(0.0, Math.min(100.0, progress)));
            if (action != null && !action.trim().isEmpty()) payload.put("action", action.trim());
            HttpResult response = request("POST", serverUrl + "/core-worker/jobs/progress", payload, token);
            if (!response.ok()) return false;
            JSONObject body = new JSONObject(response.body);
            return body.optBoolean("ok", false);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private JSONObject localCoreWorkerJobsSnapshot() throws Exception {
        JSONObject out = new JSONObject();
        JSONObject active = readActiveJob();
        String jobId = active.optString("job_id", "").trim();
        out.put("active_job_id", jobId);
        out.put("active_job_type", active.optString("type", ""));
        out.put("active_job_stage", active.optString("stage", ""));
        out.put("active_job_summary", active.optString("summary", ""));
        out.put("active_job_started_at", active.optLong("started_at", 0L));
        out.put("active_job_updated_at", active.optLong("updated_at", 0L));
        out.put("pending_result_count", pendingResultOutboxCount());
        out.put("executor_ready", prefs().getBoolean("job_executor_ready", false));
        return out;
    }

    private boolean shouldForcePoll(String reason) {
        String value = reason == null ? "" : reason.toLowerCase(java.util.Locale.ROOT);
        return value.contains("manual") || value.contains("fcm") || value.contains("resume")
                || value.contains("opened") || value.contains("status") || value.contains("diagnostic");
    }

    private JSONObject buildResultEnvelope(JSONObject job, JSONObject result) throws Exception {
        boolean ok = result != null && result.optBoolean("ok", false);
        String summary = result == null ? "resultado vazio" : compact(result.optString("message", result.optString("summary", result.optString("error", ok ? "concluído" : "falhou"))));
        JSONObject payload = new JSONObject();
        payload.put("worker_id", CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext()));
        payload.put("job_id", job == null ? "" : firstNonEmpty(job.optString("job_id", ""), job.optString("id", "")));
        payload.put("status", ok ? "succeeded" : "failed");
        payload.put("summary", summary);
        payload.put("error", ok || result == null ? "" : compact(result.optString("error", summary)));
        payload.put("result", result == null ? new JSONObject() : result);
        payload.put("queued_at", System.currentTimeMillis());
        payload.put("protocol", "core-worker-registry-v1");
        return payload;
    }

    private JSONObject normalizeStoredEnvelope(JSONObject envelope) throws Exception {
        if (envelope == null) return new JSONObject();
        if (envelope.has("worker_id") && envelope.has("job_id")) {
            String storedWorkerId = envelope.optString("worker_id", "").trim();
            String canonical = CoreWorkerRuntimeIdentity.canonicalWorkerId(getApplicationContext());
            if (CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(getApplicationContext())
                    && storedWorkerId.equals(canonical)) {
                envelope.put("worker_id", CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext()));
            }
            return envelope;
        }
        JSONObject result = envelope.optJSONObject("result");
        if (result == null) result = new JSONObject().put("ok", false).put("error", "resultado legado inválido");
        JSONObject migrated = new JSONObject();
        migrated.put("worker_id", CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext()));
        migrated.put("job_id", envelope.optString("jobId", ""));
        migrated.put("status", result.optBoolean("ok", false) ? "succeeded" : "failed");
        migrated.put("summary", compact(result.optString("message", result.optString("error", "resultado legado"))));
        migrated.put("error", result.optBoolean("ok", false) ? "" : compact(result.optString("error", "falha legada")));
        migrated.put("result", result);
        migrated.put("protocol", "core-worker-registry-v1-migrated");
        return migrated;
    }

    private File persistOutbox(String jobId, JSONObject envelope) {
        try {
            File target = outboxFile(jobId);
            if (target == null) return null;
            File temp = new File(target.getParentFile(), target.getName() + ".tmp");
            FileOutputStream output = new FileOutputStream(temp, false);
            output.write(envelope.toString().getBytes(StandardCharsets.UTF_8));
            output.flush();
            output.close();
            if (!temp.renameTo(target)) {
                FileOutputStream direct = new FileOutputStream(target, false);
                direct.write(envelope.toString().getBytes(StandardCharsets.UTF_8));
                direct.flush();
                direct.close();
                temp.delete();
            }
            return target;
        } catch (Throwable error) {
            prefs().edit().putString("agent_last_error", "outbox: " + shortThrowable(error)).apply();
            return null;
        }
    }

    private void flushResultOutbox(String serverUrl) {
        File dir = jobExecutor == null ? null : jobExecutor.outboxDir();
        File[] files = dir == null ? null : dir.listFiles();
        if (files == null) return;
        for (File file : files) {
            if (file == null || !file.isFile() || !file.getName().endsWith(".json")) continue;
            try {
                JSONObject envelope = normalizeStoredEnvelope(readJsonFile(file));
                if (postResultEnvelope(serverUrl, envelope)) {
                    String completedJobId = envelope.optString("job_id", "");
                    rememberCompletedJob(completedJobId);
                    file.delete();
                    clearActiveJob(completedJobId);
                }
            } catch (Throwable ignored) {
            }
        }
    }

    private boolean postResultEnvelope(String serverUrl, JSONObject envelope) {
        try {
            String token = prefs().getString("worker_token", "").trim();
            if (token.isEmpty()) return false;
            HttpResult response = request("POST", serverUrl + "/core-worker/jobs/result", envelope, token);
            if (!response.ok()) return false;
            JSONObject body = new JSONObject(response.body);
            return body.optBoolean("ok", false);
        } catch (Throwable error) {
            prefs().edit().putString("agent_last_error", "resultado: " + shortThrowable(error)).apply();
            return false;
        }
    }

    private File outboxFile(String jobId) {
        if (jobExecutor == null) return null;
        String safe = jobId == null ? "" : jobId.trim().replaceAll("[^a-zA-Z0-9._-]", "_");
        if (safe.isEmpty()) safe = "job-" + System.currentTimeMillis();
        return new File(jobExecutor.outboxDir(), safe + ".json");
    }

    private JSONObject readJsonFile(File file) throws Exception {
        if (file == null || !file.isFile()) return new JSONObject();
        FileInputStream input = new FileInputStream(file);
        byte[] data = new byte[(int) Math.min(file.length(), 1024L * 1024L)];
        int read = input.read(data);
        input.close();
        return read <= 0 ? new JSONObject() : new JSONObject(new String(data, 0, read, StandardCharsets.UTF_8));
    }

    private boolean wasJobRecentlyCompleted(String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return false;
        try {
            JSONArray recent = new JSONArray(prefs().getString("internal_completed_job_ids", "[]"));
            for (int i = 0; i < recent.length(); i++) if (jobId.equals(recent.optString(i, ""))) return true;
        } catch (Throwable ignored) {
        }
        return false;
    }

    private void rememberCompletedJob(String jobId) {
        if (jobId == null || jobId.trim().isEmpty()) return;
        try {
            JSONArray old = new JSONArray(prefs().getString("internal_completed_job_ids", "[]"));
            JSONArray next = new JSONArray().put(jobId);
            for (int i = 0; i < old.length() && next.length() < 32; i++) {
                String value = old.optString(i, "");
                if (!value.isEmpty() && !jobId.equals(value)) next.put(value);
            }
            prefs().edit().putString("internal_completed_job_ids", next.toString()).apply();
        } catch (Throwable ignored) {
        }
    }

    private void recordJobHistory(String type, boolean ok, String message) {
        try {
            JSONArray old = new JSONArray(prefs().getString("internal_job_history", "[]"));
            JSONArray next = new JSONArray();
            next.put(new JSONObject()
                    .put("at", System.currentTimeMillis())
                    .put("type", type == null ? "job" : type)
                    .put("ok", ok)
                    .put("message", message == null ? "" : message));
            for (int i = 0; i < old.length() && next.length() < 12; i++) {
                JSONObject item = old.optJSONObject(i);
                if (item != null) next.put(item);
            }
            prefs().edit().putString("internal_job_history", next.toString()).apply();
        } catch (Throwable ignored) {
        }
    }

    private String summarizeJobs(JSONArray jobs, int okCount, int count) {
        StringBuilder out = new StringBuilder();
        int limit = Math.min(count, 3);
        for (int i = 0; i < limit; i++) {
            JSONObject job = jobs == null ? null : jobs.optJSONObject(i);
            if (job == null) continue;
            if (out.length() > 0) out.append(", ");
            out.append(job.optString("type", "job"));
        }
        if (count > limit) out.append(" +").append(count - limit);
        if (out.length() == 0) out.append("jobs internos");
        return out.append(" · ").append(okCount).append('/').append(count).append(" ok").toString();
    }


    private String compact(String value) {
        String clean = value == null ? "" : value.replaceAll("\\s+", " ").trim();
        return clean.length() <= 600 ? clean : clean.substring(0, 600);
    }

    private String shortThrowable(Throwable error) {
        if (error == null) return "erro desconhecido";
        String message = error.getMessage() == null ? "" : error.getMessage().trim();
        String text = error.getClass().getSimpleName() + (message.isEmpty() ? "" : ": " + message);
        return text.length() <= 180 ? text : text.substring(0, 180);
    }

    private void reportHeartbeat(String reason) {
        long now = System.currentTimeMillis();
        String safeReason = reason == null || reason.trim().isEmpty() ? "foreground" : reason.trim();
        if (heartbeatRunning.get()) {
            return;
        }
        if (lastHeartbeatStartedAt > 0L && now - lastHeartbeatStartedAt < HEARTBEAT_MIN_MS && !safeReason.contains("manual")) {
            return;
        }
        lastHeartbeatStartedAt = now;
        heartbeatRunning.set(true);
        new Thread(() -> {
            try {
                String serverUrl = normalizedServerUrl();
                if (serverUrl.isEmpty()) {
                    return;
                }
                JSONObject payload = buildForegroundHeartbeatPayload(safeReason);
                String token = prefs().getString("worker_token", "").trim();
                if (token.isEmpty()) return;
                CoreWorkerRuntimeIdentity.putRuntimeFields(getApplicationContext(), payload);
                HttpResult heartbeat = request("POST", serverUrl + "/core-worker/heartbeat", payload, token);
                if (heartbeat.ok()) {
                    JSONObject body = new JSONObject(heartbeat.body);
                    if (body.optBoolean("ok", false)) {
                        SharedPreferences.Editor editor = prefs().edit()
                                .putLong("native_worker_last_heartbeat_at", System.currentTimeMillis())
                                .putString("native_worker_state", "agente autônomo online");
                        String directHttpToken = body.optString("direct_http_token", "").trim();
                        if (!directHttpToken.isEmpty()) editor.putString("direct_http_token", directHttpToken);
                        editor.apply();
                    }
                }
            } catch (Throwable ignored) {
            } finally {
                heartbeatRunning.set(false);
            }
        }, "core-worker-foreground-heartbeat").start();
    }

    private JSONObject buildForegroundHeartbeatPayload(String reason) throws Exception {
        JSONObject coreLinux = coreLinuxPublicSnapshot();
        JSONObject nativeRuntime = nativeRuntimePublicSnapshot();
        JSONArray supported = CoreWorkerJobCatalog.remoteSupportedTasks(getApplicationContext());
        JSONArray capabilities = coreWorkerApkCapabilitiesArray();
        JSONObject runtime = new JSONObject();
        runtime.put("mode", "apk-native-direct-runtime");
        runtime.put("internal_runtime", "apk-foreground-service");
        runtime.put("internal_runtime_state", "foreground-service-visible-runtime");
        runtime.put("jobs_runtime", "authenticated-registry-agent");
        runtime.put("capabilities", capabilities);
        runtime.put("supported_tasks", supported);
        runtime.put("supportedTasks", supported);
        runtime.put("foreground_runtime_active", true);
        runtime.put("foreground_runtime_summary", "serviço persistente ativo");
        runtime.put("core_linux_summary", coreLinux.optString("summary", ""));
        runtime.put("core_linux_state", coreLinux.optString("state", ""));
        runtime.put("core_linux_prepared", coreLinux.optBoolean("prepared", false));
        runtime.put("termux_required_now", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(getApplicationContext()));
        runtime.put("termux_fallback_available", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(getApplicationContext()));
        runtime.put("advanced_jobs_require_termux", false);
        runtime.put("termux_bootstrap_builder_supported", true);
        runtime.put("apk_self_builder", CoreWorkerApkBuildManager.preflight(getApplicationContext(), false));
        runtime.put("job_executor_ready", prefs().getBoolean("job_executor_ready", false));
        runtime.put("core_worker_jobs", localCoreWorkerJobsSnapshot());
        runtime.put("coreLinux", coreLinux);
        runtime.put("nativeRuntime", nativeRuntime);

        JSONObject status = new JSONObject();
        status.put("app", "foreground-agent-service");
        status.put("job_executor_ready", prefs().getBoolean("job_executor_ready", false));
        status.put("foreground_runtime_active", true);
        status.put("foreground_runtime_summary", "serviço persistente ativo");
        status.put("notification_permission", hasNotificationPermission() ? "granted" : "missing");
        status.put("termux_required_now", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(getApplicationContext()));
        status.put("bedrock_server_mode", "future-foreground-service");
        status.put("bedrock_start_allowed", coreLinux.optBoolean("bedrockStartAllowed", false));
        status.put("native_tts_bridge_active", nativeTtsServer != null);
        status.put("direct_http_active", directHttpServer != null && directHttpServer.isRunning());
        status.put("direct_http_port", CoreWorkerRuntimeIdentity.effectiveDirectHttpPort(getApplicationContext()));
        status.put("direct_http_requested_port", prefs().getInt("direct_http_requested_port", CoreWorkerRuntimeIdentity.directHttpPort(getApplicationContext())));
        status.put("direct_http_state", prefs().getString("direct_http_state", directHttpServer != null && directHttpServer.isRunning() ? "listening" : "stopped"));
        status.put("termux_replaced", !CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(getApplicationContext()));
        status.put("termux_bootstrap_active", CoreWorkerRuntimeIdentity.sharedBootstrapIdentity(getApplicationContext()));
        status.put("termux_bootstrap_builder_supported", true);
        status.put("parent_worker_id", CoreWorkerRuntimeIdentity.parentWorkerId(getApplicationContext()));
        status.put("runtime_worker_id", CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext()));
        status.put("auto_enrollment_state", prefs().getString("auto_enrollment_state", CoreWorkerAutoEnrollment.supported() ? "waiting_parent" : "disabled"));
        status.put("auto_enrolled_apk", prefs().getBoolean("auto_enrolled_apk", false));
        status.put("source_fingerprint", BuildConfig.CORE_WORKER_SOURCE_FINGERPRINT);
        status.put("apk_self_builder", CoreWorkerApkBuildManager.preflight(getApplicationContext(), false));
        status.put("core_worker_jobs", localCoreWorkerJobsSnapshot());
        status.put("capabilities", capabilities);
        status.put("supported_tasks", supported);
        status.put("supportedTasks", supported);
        status.put("core_linux_summary", coreLinux.optString("summary", ""));
        status.put("core_linux_state", coreLinux.optString("state", ""));
        status.put("core_linux_prepared", coreLinux.optBoolean("prepared", false));
        status.put("coreLinux", coreLinux);
        status.put("nativeRuntime", nativeRuntime);
        if (nativeTtsManager != null) {
            status.put("android_tts", nativeTtsManager.statusJson());
        }

        JSONObject payload = new JSONObject();
        CoreWorkerRuntimeIdentity.putRuntimeFields(getApplicationContext(), payload);
        payload.put("name", prefs().getString("device_name", Build.MANUFACTURER + " " + Build.MODEL));
        payload.put("version", BuildConfig.VERSION_NAME);
        payload.put("endpoint", prefs().getString("direct_worker_endpoint", ""));
        payload.put("roles", CoreWorkerJobCatalog.roles(getApplicationContext()));
        payload.put("platform", "android");
        payload.put("source", "core-worker-apk-foreground-service");
        payload.put("state", "foreground_runtime");
        payload.put("reason", reason == null || reason.trim().isEmpty() ? "foreground" : reason.trim());
        payload.put("appVersion", BuildConfig.VERSION_NAME);
        payload.put("appVersionCode", BuildConfig.VERSION_CODE);
        payload.put("versionName", BuildConfig.VERSION_NAME);
        payload.put("versionCode", BuildConfig.VERSION_CODE);
        payload.put("sourceFingerprint", BuildConfig.CORE_WORKER_SOURCE_FINGERPRINT);
        payload.put("workerId", CoreWorkerRuntimeIdentity.runtimeWorkerId(getApplicationContext()));
        payload.put("installId", installId());
        payload.put("deviceName", prefs().getString("device_name", ""));
        payload.put("runtime_mode", "apk-native-python-linux-assisted-runtime");
        payload.put("jobsRuntime", "foreground-service-autonomous-agent");
        payload.put("internal_runtime", "apk-foreground-service");
        payload.put("internal_runtime_state", "foreground-service-visible-runtime");
        payload.put("capabilities", capabilities);
        payload.put("supported_tasks", supported);
        payload.put("supportedTasks", supported);
        payload.put("app_jobs", supported);
        payload.put("coreLinux", coreLinux);
        payload.put("nativeRuntime", nativeRuntime);
        payload.put("runtime", runtime);
        payload.put("status", status);
        return payload;
    }

    private JSONArray coreWorkerApkCapabilitiesArray() {
        return CoreWorkerJobCatalog.capabilities(getApplicationContext());
    }

    private JSONArray supportedLightJobsArray() {
        return CoreWorkerJobCatalog.remoteSupportedTasks(getApplicationContext());
    }

    private JSONObject coreLinuxPublicSnapshot() {
        File runtimeDir = new File(getFilesDir(), "core-linux/runtime");
        JSONObject runtime = readJson(new File(runtimeDir, "linux-runtime-state.json"));
        JSONObject rootfs = readJson(new File(runtimeDir, "rootfs-state.json"));
        JSONObject smoke = readJson(new File(runtimeDir, "core-linux-smoke-test.json"));
        JSONObject rootfsImport = readJson(new File(runtimeDir, "rootfs-import-state.json"));
        JSONObject runner = readJson(new File(runtimeDir, "runner-preflight-state.json"));

        JSONObject importAction = rootfsImport.optJSONObject("import");
        JSONObject importValidation = importAction == null ? null : importAction.optJSONObject("validation");
        if (importValidation == null) importValidation = rootfsImport.optJSONObject("validation");
        JSONObject rootfsValidation = rootfs.optJSONObject("validation");

        String importState = firstNonEmpty(
                importAction == null ? "" : importAction.optString("state", ""),
                rootfsImport.optString("state", "")
        );
        String rootfsState = rootfs.optString("state", "");
        String level = firstNonEmpty(
                importValidation == null ? "" : importValidation.optString("validationLevel", ""),
                rootfsValidation == null ? "" : rootfsValidation.optString("validationLevel", ""),
                rootfs.optString("validationLevel", ""), rootfs.optString("rootfsValidationLevel", ""),
                rootfsImport.optString("validationLevel", "")
        );
        boolean strictKnown = "real".equalsIgnoreCase(level)
                || importState.toLowerCase(java.util.Locale.ROOT).contains("glibc")
                || importState.toLowerCase(java.util.Locale.ROOT).contains("rootfs_real")
                || (importValidation != null && importValidation.has("glibcRuntime"));
        boolean strictFailure = strictKnown && (
                importState.toLowerCase(java.util.Locale.ROOT).contains("failed")
                || importState.toLowerCase(java.util.Locale.ROOT).contains("invalid")
                || (importValidation != null && !importValidation.optBoolean("ok", false))
        );
        boolean strictSuccess = strictKnown && !strictFailure
                && importValidation != null && importValidation.optBoolean("ok", false)
                && (importValidation.optJSONObject("glibcRuntime") == null
                    || importValidation.optJSONObject("glibcRuntime").optBoolean("ok", false));
        boolean legacyRealSuccess = !strictKnown
                && (rootfsState.toLowerCase(java.util.Locale.ROOT).contains("rootfs_real_validated")
                    || ("real".equalsIgnoreCase(level) && rootfsValidation != null && rootfsValidation.optBoolean("ok", false)));
        boolean realValidated = !strictFailure && (strictSuccess || legacyRealSuccess);

        boolean prepared = realValidated;
        boolean rootfsReady = realValidated;
        boolean distributionReady = realValidated;
        boolean runnerReady = realValidated && runner.optBoolean("runnerReady", false)
                && runner.optBoolean("runnerRequirementsReady", false);
        boolean runnerExecutionAllowed = runnerReady && runner.optBoolean("runnerExecutionAllowed", false);
        boolean bedrockStartAllowed = runnerExecutionAllowed && runner.optBoolean("bedrockRequirementsReady", false);
        JSONArray missing = new JSONArray();
        if (strictFailure && importValidation != null && importValidation.optJSONArray("missing") != null) {
            JSONArray source = importValidation.optJSONArray("missing");
            for (int i = 0; i < source.length(); i++) missing.put(source.opt(i));
        } else if (runner.optJSONArray("missing") != null) {
            JSONArray source = runner.optJSONArray("missing");
            for (int i = 0; i < source.length(); i++) missing.put(source.opt(i));
        }

        String state;
        String summary;
        if (strictFailure) {
            state = firstNonEmpty(importState, "rootfs_real_validation_failed");
            summary = firstNonEmpty(
                    importAction == null ? "" : importAction.optString("summary", ""),
                    rootfsImport.optString("summary", ""),
                    importValidation == null ? "" : importValidation.optString("summary", ""),
                    "Rootfs real reprovado na validação glibc"
            );
        } else if (realValidated) {
            state = "rootfs_real_validated";
            summary = firstNonEmpty(rootfs.optString("summary", ""), rootfsImport.optString("summary", ""), "Rootfs real validado");
        } else {
            state = firstNonEmpty(importState, rootfsState, runtime.optString("state", ""), smoke.optString("state", ""), "runtime_v1_pending");
            summary = firstNonEmpty(rootfsImport.optString("summary", ""), rootfs.optString("summary", ""), runtime.optString("summary", ""), "Core Linux aguardando rootfs real com glibc arm64");
        }

        JSONObject out = new JSONObject();
        try {
            out.put("summary", summary);
            out.put("state", state);
            out.put("prepared", prepared);
            out.put("rootfsReady", rootfsReady);
            out.put("executorReady", runnerExecutionAllowed);
            out.put("lastCheckAt", Math.max(Math.max(runtime.optLong("updatedAt", 0L), runner.optLong("updatedAt", 0L)), Math.max(rootfs.optLong("updatedAt", 0L), rootfsImport.optLong("updatedAt", 0L))));
            out.put("termuxRequired", false);
            out.put("termuxReplaced", true);
            out.put("bedrockStartAllowed", bedrockStartAllowed);
            out.put("rootfsValidationLevel", strictKnown ? "real" : level);
            out.put("rootfsDistributionReady", distributionReady);
            out.put("readyForBox64Install", realValidated);
            out.put("readyForBox64Smoke", realValidated && runnerReady);
            out.put("readyForBedrockStart", bedrockStartAllowed);
            out.put("strictValidationKnown", strictKnown);
            out.put("strictValidationFailed", strictFailure);
            out.put("rootfsState", state);
            out.put("rootfsSummary", summary);
            out.put("rootfsImportState", importState);
            out.put("rootfsImportSummary", rootfsImport.optString("summary", ""));
            out.put("blockers", missing);
            out.put("runnerPreflightState", runner.optString("state", ""));
            out.put("runnerPreflightSummary", runner.optString("summary", ""));
            out.put("runnerPreflightVersion", runner.optInt("preflightVersion", 1));
            out.put("runnerReady", runnerReady);
            out.put("runnerBlocked", !runnerExecutionAllowed);
            out.put("runnerExecutionAllowed", runnerExecutionAllowed);
            out.put("runnerRequirementsReady", realValidated && runner.optBoolean("runnerRequirementsReady", false));
            out.put("runnerMissing", missing);
            if (runner.length() > 0) out.put("runnerPreflight", runner);
            out.put("supportedStage", strictFailure ? "core-linux-rootfs-glibc-intake-preflight-v17" : (realValidated ? "core-linux-rootfs-import-v1" : "core-linux-runtime-v1-smoke"));
            out.put("supportedTasks", supportedLightJobsArray());
            if (runtime.length() > 0) out.put("runtime", runtime);
            if (rootfs.length() > 0) out.put("rootfs", rootfs);
            if (rootfsImport.length() > 0) out.put("rootfsImport", rootfsImport);
            if (smoke.length() > 0) out.put("smoke", smoke);
        } catch (Throwable ignored) { }
        return out;
    }

    private JSONObject nativeRuntimePublicSnapshot() {
        JSONObject executor = readJson(new File(new File(getFilesDir(), "core-linux/runtime"), "native-executor-state.json"));
        JSONObject out = new JSONObject();
        try {
            out.put("summary", firstNonEmpty(executor.optString("summary", ""), executor.optBoolean("readyForRootfs", false) ? "executor nativo interno pronto para rootfs" : "executor nativo aguardando teste"));
            out.put("workerOnline", true);
            out.put("workerState", executor.optBoolean("readyForRootfs", false) ? "ready" : "pending");
            out.put("pythonAvailable", false);
            out.put("lastHeartbeatAt", executor.optLong("updatedAt", 0L));
            out.put("supportedTasks", supportedLightJobsArray());
            if (executor.length() > 0) out.put("executor", executor);
        } catch (Throwable ignored) {
        }
        return out;
    }

    private JSONObject readJson(File file) {
        try {
            if (file == null || !file.isFile()) return new JSONObject();
            FileInputStream input = new FileInputStream(file);
            byte[] data = new byte[(int) Math.min(file.length(), 512L * 1024L)];
            int read = input.read(data);
            input.close();
            if (read <= 0) return new JSONObject();
            return new JSONObject(new String(data, 0, read, StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) return value.trim();
        }
        return "";
    }

    private boolean hasNotificationPermission() {
        return Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private String installId() {
        String id = prefs().getString("install_id", "");
        if (id == null || id.trim().isEmpty()) {
            id = UUID.randomUUID().toString();
            prefs().edit().putString("install_id", id).apply();
        }
        return id;
    }

    private String normalizedServerUrl() {
        String url = prefs().getString("server_url", "").trim();
        if (url.isEmpty()) {
            url = BuildConfig.CORE_WORKER_VPS_URL == null ? "" : BuildConfig.CORE_WORKER_VPS_URL.trim();
        }
        return url.replaceAll("/+$", "");
    }

    private HttpResult request(String method, String url, JSONObject payload) throws Exception {
        return request(method, url, payload, null);
    }

    private HttpResult request(String method, String url, JSONObject payload, String token) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(7000);
        conn.setReadTimeout(12000);
        conn.setRequestProperty("Accept", "application/json");
        if (token != null && !token.trim().isEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer " + token.trim());
        }
        if (payload != null) {
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            OutputStream output = conn.getOutputStream();
            output.write(payload.toString().getBytes(StandardCharsets.UTF_8));
            output.flush();
            output.close();
        }
        int status = conn.getResponseCode();
        InputStream input = status >= 200 && status < 400 ? conn.getInputStream() : conn.getErrorStream();
        String body = readAll(input);
        conn.disconnect();
        return new HttpResult(status, body == null ? "" : body);
    }

    private String readAll(InputStream input) throws Exception {
        if (input == null) return "";
        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        StringBuilder builder = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            if (builder.length() > 0) builder.append('\n');
            builder.append(line);
        }
        reader.close();
        return builder.toString();
    }

    private static final class HttpResult {
        final int status;
        final String body;

        HttpResult(int status, String body) {
            this.status = status;
            this.body = body;
        }

        boolean ok() {
            return status >= 200 && status < 300;
        }
    }
}
