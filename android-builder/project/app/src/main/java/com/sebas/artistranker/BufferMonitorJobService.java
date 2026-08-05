package com.sebas.artistranker;

import android.app.job.JobParameters;
import android.app.job.JobService;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class BufferMonitorJobService extends JobService {
    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    @Override
    public boolean onStartJob(JobParameters params) {
        executor.execute(() -> {
            if (!BufferMonitorSupport.notificationsEnabled(this) || BufferMonitorSupport.isAppForeground(this)) {
                jobFinished(params, false);
                return;
            }
            BufferMonitorSupport.QueryResult result = BufferMonitorSupport.query(this);
            boolean finished = result.success && BufferMonitorSupport.handleBackgroundResult(this, result);
            if (!finished) BufferMonitorSupport.scheduleFallback(this);
            jobFinished(params, false);
        });
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        BufferMonitorSupport.scheduleFallback(this);
        return false;
    }

    @Override
    public void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }
}
