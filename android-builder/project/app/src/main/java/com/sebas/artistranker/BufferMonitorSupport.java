package com.sebas.artistranker;

import android.Manifest;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class BufferMonitorSupport {
    static final String PREFS = "artist_ranker_app";
    static final String PREF_LAST_SUCCESS = "last_successful_base";
    static final String PREF_NOTIFICATIONS = "buffer_full_notifications";
    static final String PREF_APP_FOREGROUND = "app_foreground";
    static final String PREF_BASELINE_KNOWN = "buffer_baseline_known";
    static final String PREF_LAST_FULL = "buffer_last_full";
    static final String PREF_LAST_READY = "buffer_last_ready";
    static final String PREF_LAST_TARGET = "buffer_last_target";
    static final String DEFAULT_BASE = "http://artist-ranker.local:7860";
    static final String READY_CHANNEL = "buffer_full";
    static final String MONITOR_CHANNEL = "buffer_monitor";
    static final int READY_NOTIFICATION_ID = 1101;
    static final int MONITOR_NOTIFICATION_ID = 1102;
    static final int FALLBACK_JOB_ID = 7103;
    static final long FALLBACK_DELAY_MS = 15L * 60L * 1000L;

    private BufferMonitorSupport() {}

    static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static void setAppForeground(Context context, boolean foreground) {
        preferences(context).edit().putBoolean(PREF_APP_FOREGROUND, foreground).apply();
    }

    static boolean isAppForeground(Context context) {
        return preferences(context).getBoolean(PREF_APP_FOREGROUND, false);
    }

    static boolean notificationsEnabled(Context context) {
        return preferences(context).getBoolean(PREF_NOTIFICATIONS, true);
    }

    static void recordBufferState(Context context, int ready, int target) {
        if (target <= 0) return;
        preferences(context).edit()
                .putBoolean(PREF_BASELINE_KNOWN, true)
                .putBoolean(PREF_LAST_FULL, ready >= target)
                .putInt(PREF_LAST_READY, ready)
                .putInt(PREF_LAST_TARGET, target)
                .apply();
    }

    static void startMonitoring(Context context) {
        Context app = context.getApplicationContext();
        if (!notificationsEnabled(app) || isAppForeground(app)) return;
        ensureChannels(app);
        scheduleFallback(app);
        Intent intent = new Intent(app, BufferMonitorService.class);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) app.startForegroundService(intent);
            else app.startService(intent);
        } catch (RuntimeException ignored) {
            scheduleFallback(app);
        }
    }

    static void stopMonitoring(Context context) {
        Context app = context.getApplicationContext();
        try { app.stopService(new Intent(app, BufferMonitorService.class)); } catch (RuntimeException ignored) {}
        JobScheduler scheduler = (JobScheduler) app.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (scheduler != null) scheduler.cancel(FALLBACK_JOB_ID);
        NotificationManager manager = (NotificationManager) app.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.cancel(MONITOR_NOTIFICATION_ID);
    }

    static void cancelFallback(Context context) {
        JobScheduler scheduler = (JobScheduler) context.getApplicationContext().getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (scheduler != null) scheduler.cancel(FALLBACK_JOB_ID);
    }

    static void scheduleFallback(Context context) {
        Context app = context.getApplicationContext();
        if (!notificationsEnabled(app) || isAppForeground(app)) return;
        JobScheduler scheduler = (JobScheduler) app.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (scheduler == null) return;
        JobInfo job = new JobInfo.Builder(FALLBACK_JOB_ID, new ComponentName(app, BufferMonitorJobService.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setMinimumLatency(FALLBACK_DELAY_MS)
                .setOverrideDeadline(FALLBACK_DELAY_MS * 2L)
                .setPersisted(true)
                .build();
        scheduler.schedule(job);
    }

    static QueryResult query(Context context) {
        String base = preferences(context).getString(PREF_LAST_SUCCESS, DEFAULT_BASE);
        if (base == null || base.trim().isEmpty()) base = DEFAULT_BASE;
        HttpURLConnection connection = null;
        try {
            URL url = new URL(base + "/api/duel/metrics");
            connection = (HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(5000);
            connection.setUseCaches(false);
            connection.setRequestProperty("Accept", "application/json");
            String cookie = CookieManager.getInstance().getCookie(base);
            if (cookie != null && !cookie.trim().isEmpty()) {
                connection.setRequestProperty("Cookie", cookie);
            }
            if (connection.getResponseCode() != 200) return QueryResult.error();
            StringBuilder body = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(connection.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) body.append(line);
            }
            JSONObject root = new JSONObject(body.toString());
            JSONObject buffer = root.optJSONObject("buffer");
            if (buffer == null) return QueryResult.error();
            return new QueryResult(true, buffer.optInt("total_ready", 0), buffer.optInt("target", 0));
        } catch (Exception ignored) {
            return QueryResult.error();
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    static boolean handleBackgroundResult(Context context, QueryResult result) {
        if (!result.success || result.target <= 0) return false;
        SharedPreferences prefs = preferences(context);
        boolean full = result.ready >= result.target;
        boolean known = prefs.getBoolean(PREF_BASELINE_KNOWN, false);
        boolean previousFull = prefs.getBoolean(PREF_LAST_FULL, false);
        prefs.edit()
                .putBoolean(PREF_BASELINE_KNOWN, true)
                .putBoolean(PREF_LAST_FULL, full)
                .putInt(PREF_LAST_READY, result.ready)
                .putInt(PREF_LAST_TARGET, result.target)
                .apply();
        if (isAppForeground(context)) {
            cancelReadyNotification(context);
            return true;
        }
        if (full) {
            if (known && !previousFull) showReadyNotification(context, result.ready, result.target);
            return true;
        }
        return false;
    }

    static Notification buildMonitorNotification(Context context, int ready, int target) {
        ensureChannels(context);
        String text = target > 0 ? "Waiting for duel buffer • " + ready + " of " + target : "Watching the duel buffer on your PC";
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(context, MONITOR_CHANNEL)
                : new Notification.Builder(context);
        return builder.setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("Artist Ranker is watching the buffer")
                .setContentText(text)
                .setContentIntent(appPendingIntent(context))
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setCategory(Notification.CATEGORY_SERVICE)
                .build();
    }

    static void updateMonitorNotification(Context context, int ready, int target) {
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.notify(MONITOR_NOTIFICATION_ID, buildMonitorNotification(context, ready, target));
    }

    static void showReadyNotification(Context context, int ready, int target) {
        if (isAppForeground(context) || !notificationsEnabled(context)) return;
        if (Build.VERSION.SDK_INT >= 33 && context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return;
        ensureChannels(context);
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(context, READY_CHANNEL)
                : new Notification.Builder(context);
        Notification notification = builder.setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("Artist Ranker buffer is ready")
                .setContentText(ready + " of " + target + " duels are buffered.")
                .setContentIntent(appPendingIntent(context))
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
                .setCategory(Notification.CATEGORY_STATUS)
                .build();
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.notify(READY_NOTIFICATION_ID, notification);
    }

    static void cancelReadyNotification(Context context) {
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.cancel(READY_NOTIFICATION_ID);
    }

    private static PendingIntent appPendingIntent(Context context) {
        Intent intent = new Intent(context, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    static void ensureChannels(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) return;
        NotificationChannel ready = new NotificationChannel(READY_CHANNEL, "Duel buffer ready", NotificationManager.IMPORTANCE_DEFAULT);
        ready.setDescription("Alerts when the configured duel buffer reaches its maximum.");
        manager.createNotificationChannel(ready);
        NotificationChannel monitor = new NotificationChannel(MONITOR_CHANNEL, "Buffer background monitor", NotificationManager.IMPORTANCE_LOW);
        monitor.setDescription("Quiet status shown only while Artist Ranker watches the buffer in the background.");
        monitor.setShowBadge(false);
        manager.createNotificationChannel(monitor);
    }

    static final class QueryResult {
        final boolean success;
        final int ready;
        final int target;
        QueryResult(boolean success, int ready, int target) { this.success = success; this.ready = ready; this.target = target; }
        static QueryResult error() { return new QueryResult(false, 0, 0); }
    }
}
