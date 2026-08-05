package com.sebas.artistranker;

import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

public final class BufferMonitorService extends Service {
    private ScheduledExecutorService executor;

    @Override
    public void onCreate() {
        super.onCreate();
        BufferMonitorSupport.ensureChannels(this);
        startForeground(BufferMonitorSupport.MONITOR_NOTIFICATION_ID, BufferMonitorSupport.buildMonitorNotification(this, 0, 0));
        executor = Executors.newSingleThreadScheduledExecutor();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!BufferMonitorSupport.notificationsEnabled(this) || BufferMonitorSupport.isAppForeground(this)) {
            stopMonitor(false);
            return START_NOT_STICKY;
        }
        if (executor != null) {
            executor.shutdownNow();
            executor = Executors.newSingleThreadScheduledExecutor();
        }
        executor.scheduleWithFixedDelay(this::poll, 0L, 10L, TimeUnit.SECONDS);
        BufferMonitorSupport.scheduleFallback(this);
        return START_STICKY;
    }

    private void poll() {
        if (!BufferMonitorSupport.notificationsEnabled(this) || BufferMonitorSupport.isAppForeground(this)) {
            stopMonitor(false);
            return;
        }
        BufferMonitorSupport.QueryResult result = BufferMonitorSupport.query(this);
        if (!result.success) return;
        BufferMonitorSupport.updateMonitorNotification(this, result.ready, result.target);
        if (BufferMonitorSupport.handleBackgroundResult(this, result)) stopMonitor(false);
    }

    private void stopMonitor(boolean keepFallback) {
        if (keepFallback) BufferMonitorSupport.scheduleFallback(this);
        else BufferMonitorSupport.cancelFallback(this);
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        BufferMonitorSupport.scheduleFallback(this);
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        if (executor != null) executor.shutdownNow();
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) manager.cancel(BufferMonitorSupport.MONITOR_NOTIFICATION_ID);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
