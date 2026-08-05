package com.sebas.artistranker;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Rect;
import android.graphics.drawable.GradientDrawable;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.provider.Settings;
import android.view.Gravity;
import android.view.HapticFeedbackConstants;
import android.view.MenuItem;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ProgressBar;
import android.widget.Space;
import android.widget.ScrollView;
import android.widget.TextView;

import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends Activity implements NativeConnectionController.Callback {
    private static final String DEFAULT_BASE = "http://artist-ranker.local:7860";
    private static final String PREFS = "artist_ranker_app";
    private static final String PREF_LAST_SUCCESS = "last_successful_base";
    private static final String PREF_SOUND = "sound_enabled";
    private static final String PREF_HAPTICS = "both_bad_haptics";
    private static final String PREF_NOTIFICATIONS = "buffer_full_notifications";
    private static final String PREF_HIDE_STATUS = "hide_status_bar";
    private static final String PREF_SHOW_HELP = "show_floating_help_button";
    private static final String PREF_NATIVE_PAIRING_TOUR = "native_pairing_tour_version";
    private static final String PREF_NATIVE_APP_TOUR = "native_app_tour_version";
    private static final int NATIVE_TOUR_VERSION = 4;
    private static final int REQUEST_NOTIFICATIONS = 2101;
    private static final int MENU_DUEL = 1;
    private static final int MENU_RANKER = 2;
    private static final int MENU_RELOAD = 3;
    private static final int MENU_SERVER = 4;
    private static final int MENU_FORGET_PAIRING = 5;
    private static final int MENU_SOUND = 10;
    private static final int MENU_HAPTICS = 11;
    private static final int MENU_NOTIFICATIONS = 12;
    private static final int MENU_HIDE_STATUS = 13;
    private static final int MENU_SHOW_HELP = 14;
    private static final int MENU_HELP_SETUP = 20;
    private static final int MENU_RESTART_TOUR = 21;
    private static final int MENU_TROUBLESHOOTING = 22;
    private static final long KEEP_AWAKE_AFTER_ACTIVITY_MS = 10L * 60L * 1000L;

    private SharedPreferences preferences;
    private FrameLayout root;
    private WebView webView;
    private ScrollView connectionScroll;
    private LinearLayout connectionPanel;
    private TextView connectionTitle;
    private TextView connectionMessage;
    private ProgressBar connectionProgress;
    private Button useLastButton;
    private Button menuButton;
    private LinearLayout nativePairingControls;
    private TextView nativeSelectedServer;
    private TextView nativeDiscoveryStatus;
    private LinearLayout nativeServerList;
    private EditText nativePairingCode;
    private Button nativePairButton;
    private Button nativeManualAddressButton;
    private NativeConnectionController connectionController;
    private NativeGuideOverlay nativeGuideOverlay;
    private String selectedPairingBase = DEFAULT_BASE;
    private String attemptedBase = DEFAULT_BASE;
    private boolean isLoading;
    private boolean mainFrameFailed;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private boolean appInForeground;
    private boolean autoDiscoveredConnectAttempted;
    private List<NativeConnectionController.ServerCandidate> lastDiscoveredServers = new ArrayList<>();

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable clearKeepAwake = () ->
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    private final Runnable reconnectRunnable = () -> {
        if (connectionScroll != null && connectionScroll.getVisibility() == View.VISIBLE && !isLoading) {
            attemptBase(attemptedBase == null ? DEFAULT_BASE : attemptedBase, true);
        }
    };


    private final class NativeBridge {
        @JavascriptInterface
        public void haptic(String kind) {
            if (!"both_bad".equalsIgnoreCase(String.valueOf(kind))) return;
            handler.post(MainActivity.this::performBothBadHaptic);
        }

        @JavascriptInterface
        public void setSoundEnabled(boolean enabled) {
            if (preferences != null) preferences.edit().putBoolean(PREF_SOUND, enabled).apply();
        }

        @JavascriptInterface
        public void bufferState(int ready, int target) {
            handler.post(() -> handleBufferState(ready, target));
        }

        @JavascriptInterface
        public void pairingRequired(String message) {
            handler.post(() -> showNativePairing(
                    attemptedBase == null ? DEFAULT_BASE : attemptedBase,
                    "Artist Ranker",
                    message == null || message.trim().isEmpty()
                            ? "This device must be paired again."
                            : message));
        }

        @JavascriptInterface
        public void openNativeHelp() {
            handler.post(MainActivity.this::showHelpSetup);
        }

        @JavascriptInterface
        public void setHelpButtonVisible(boolean visible) {
            handler.post(() -> {
                preferences.edit().putBoolean(PREF_SHOW_HELP, visible).apply();
                syncPagePreferences();
            });
        }

        @JavascriptInterface
        public void restartNativeTour() {
            handler.post(MainActivity.this::restartNativeTour);
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        preferences = getSharedPreferences(PREFS, MODE_PRIVATE);
        ensurePreferenceDefaults();
        getWindow().setStatusBarColor(Color.rgb(11, 14, 18));
        getWindow().setNavigationBarColor(Color.rgb(11, 14, 18));
        buildInterface();
        configureWebView();
        connectionController = new NativeConnectionController(this, this);
        connectionController.startDiscovery();
        BufferMonitorSupport.ensureChannels(this);
        applyStatusBarPreference();
        registerNetworkCallback();
        handler.postDelayed(this::requestNotificationPermissionIfNeeded, 700L);
        BufferMonitorSupport.setAppForeground(this, true);
        if (!handleDeepLinkIntent(getIntent())) {
            String initialBase = preferences.getString(PREF_LAST_SUCCESS, DEFAULT_BASE);
            attemptBase(initialBase == null || initialBase.isEmpty() ? DEFAULT_BASE : initialBase, false);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleDeepLinkIntent(intent);
    }

    private boolean handleDeepLinkIntent(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null
                || !"artist-ranker".equalsIgnoreCase(data.getScheme())
                || !"pair".equalsIgnoreCase(data.getHost())) {
            return false;
        }
        String server = data.getQueryParameter("server");
        String code = data.getQueryParameter("code");
        final String normalized;
        try {
            normalized = normalizeBase(server);
        } catch (RuntimeException error) {
            showConnectionFailure("The pairing QR contains an invalid server address.");
            return true;
        }
        String cleanCode = code == null ? "" : code.trim().toUpperCase(Locale.US);
        selectedPairingBase = normalized;
        attemptedBase = normalized;
        showNativePairing(normalized, "Artist Ranker",
                cleanCode.isEmpty()
                        ? "Enter the one-use pairing code shown on the PC."
                        : "Pairing link received. Confirming the one-use code…");
        if (nativePairingCode != null) nativePairingCode.setText(cleanCode);
        if (!cleanCode.isEmpty() && connectionController != null) {
            connectionController.addManualCandidate(normalized, "Artist Ranker", "Opened from pairing QR");
            connectionController.pair(normalized, cleanCode);
        }
        return true;
    }

    private void buildInterface() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(11, 14, 18));

        webView = new WebView(this);
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        menuButton = new Button(this);
        menuButton.setText("⋮");
        menuButton.setTextSize(22f);
        menuButton.setTextColor(Color.WHITE);
        menuButton.setAllCaps(false);
        menuButton.setPadding(0, 0, 0, 4);
        menuButton.setBackground(roundRect(Color.argb(218, 18, 24, 34), 14));
        menuButton.setContentDescription("Artist Ranker app menu");
        menuButton.setOnClickListener(this::showAppMenu);
        FrameLayout.LayoutParams menuParams = new FrameLayout.LayoutParams(dp(44), dp(44), Gravity.TOP | Gravity.START);
        menuParams.setMargins(dp(8), dp(8), 0, 0);
        root.addView(menuButton, menuParams);

        connectionScroll = new ScrollView(this);
        connectionScroll.setFillViewport(true);
        connectionScroll.setBackgroundColor(Color.rgb(11, 14, 18));
        connectionPanel = new LinearLayout(this);
        connectionPanel.setOrientation(LinearLayout.VERTICAL);
        connectionPanel.setGravity(Gravity.CENTER_HORIZONTAL);
        connectionPanel.setPadding(dp(28), dp(36), dp(28), dp(28));
        connectionPanel.setBackgroundColor(Color.rgb(11, 14, 18));

        Space topSpace = new Space(this);
        connectionPanel.addView(topSpace, new LinearLayout.LayoutParams(1, dp(28)));

        TextView icon = new TextView(this);
        icon.setText("AR");
        icon.setTextSize(27f);
        icon.setGravity(Gravity.CENTER);
        icon.setTextColor(Color.rgb(238, 243, 251));
        icon.setBackground(roundRect(Color.rgb(28, 35, 48), 20, Color.rgb(112, 168, 255), dp(2)));
        connectionPanel.addView(icon, new LinearLayout.LayoutParams(dp(78), dp(78)));

        connectionTitle = new TextView(this);
        connectionTitle.setText("Connecting to Artist Ranker");
        connectionTitle.setTextSize(22f);
        connectionTitle.setTextColor(Color.rgb(238, 243, 251));
        connectionTitle.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams titleParams = wrapParams();
        titleParams.setMargins(0, dp(22), 0, 0);
        connectionPanel.addView(connectionTitle, titleParams);

        connectionMessage = new TextView(this);
        connectionMessage.setText("Trying " + DEFAULT_BASE);
        connectionMessage.setTextSize(15f);
        connectionMessage.setTextColor(Color.rgb(154, 168, 188));
        connectionMessage.setGravity(Gravity.CENTER);
        connectionMessage.setLineSpacing(0f, 1.15f);
        LinearLayout.LayoutParams messageParams = matchWrapParams();
        messageParams.setMargins(0, dp(10), 0, dp(16));
        connectionPanel.addView(connectionMessage, messageParams);

        connectionProgress = new ProgressBar(this);
        connectionPanel.addView(connectionProgress, new LinearLayout.LayoutParams(dp(40), dp(40)));

        nativePairingControls = buildNativePairingControls();
        nativePairingControls.setVisibility(View.GONE);
        LinearLayout.LayoutParams nativeControlsParams = matchWrapParams();
        nativeControlsParams.setMargins(0, dp(14), 0, 0);
        connectionPanel.addView(nativePairingControls, nativeControlsParams);

        Space bottomSpace = new Space(this);
        connectionPanel.addView(bottomSpace, new LinearLayout.LayoutParams(1, dp(28)));

        connectionScroll.addView(connectionPanel, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(connectionScroll, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        nativeGuideOverlay = new NativeGuideOverlay(this);
        root.addView(nativeGuideOverlay, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
        updateLastSuccessfulButton();
    }

    private LinearLayout buildNativePairingControls() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER_HORIZONTAL);

        nativeSelectedServer = new TextView(this);
        nativeSelectedServer.setText("Selected server: " + DEFAULT_BASE);
        nativeSelectedServer.setTextSize(14f);
        nativeSelectedServer.setTextColor(Color.rgb(238, 243, 251));
        nativeSelectedServer.setGravity(Gravity.CENTER);
        nativeSelectedServer.setPadding(dp(10), dp(9), dp(10), dp(9));
        nativeSelectedServer.setBackground(roundRect(
                Color.rgb(24, 33, 50), 12, Color.rgb(70, 101, 143), dp(1)));
        panel.addView(nativeSelectedServer, matchWrapParams());

        nativeDiscoveryStatus = new TextView(this);
        nativeDiscoveryStatus.setText("Searching the local network for Artist Ranker…");
        nativeDiscoveryStatus.setTextSize(12f);
        nativeDiscoveryStatus.setTextColor(Color.rgb(154, 168, 188));
        nativeDiscoveryStatus.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams discoveryParams = matchWrapParams();
        discoveryParams.setMargins(0, dp(8), 0, 0);
        panel.addView(nativeDiscoveryStatus, discoveryParams);

        nativeServerList = new LinearLayout(this);
        nativeServerList.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams listParams = matchWrapParams();
        listParams.setMargins(0, dp(6), 0, 0);
        panel.addView(nativeServerList, listParams);

        TextView codeLabel = new TextView(this);
        codeLabel.setText("One-use pairing code");
        codeLabel.setTextSize(13f);
        codeLabel.setTextColor(Color.rgb(190, 202, 219));
        LinearLayout.LayoutParams codeLabelParams = matchWrapParams();
        codeLabelParams.setMargins(0, dp(12), 0, dp(5));
        panel.addView(codeLabel, codeLabelParams);

        nativePairingCode = new EditText(this);
        nativePairingCode.setSingleLine(true);
        nativePairingCode.setHint("ABC123");
        nativePairingCode.setTextSize(21f);
        nativePairingCode.setTextColor(Color.rgb(238, 243, 251));
        nativePairingCode.setHintTextColor(Color.rgb(104, 119, 143));
        nativePairingCode.setGravity(Gravity.CENTER);
        nativePairingCode.setAllCaps(true);
        nativePairingCode.setInputType(android.text.InputType.TYPE_CLASS_TEXT
                | android.text.InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS);
        nativePairingCode.setPadding(dp(10), 0, dp(10), 0);
        nativePairingCode.setBackground(roundRect(
                Color.rgb(16, 24, 38), 12, Color.rgb(70, 101, 143), dp(1)));
        panel.addView(nativePairingCode, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(50)));

        nativePairButton = actionButton("Pair this Android app", true);
        nativePairButton.setOnClickListener(v -> {
            if (connectionController == null) return;
            connectionController.pair(selectedPairingBase, nativePairingCode.getText().toString());
        });
        panel.addView(nativePairButton, buttonParams(dp(8)));

        Button retrySelected = actionButton("Retry selected server", false);
        retrySelected.setOnClickListener(v -> attemptBase(selectedPairingBase, false));
        panel.addView(retrySelected, buttonParams(dp(8)));

        Button rediscover = actionButton("Search local network again", false);
        rediscover.setOnClickListener(v -> {
            autoDiscoveredConnectAttempted = false;
            if (connectionController != null) connectionController.startDiscovery();
        });
        panel.addView(rediscover, buttonParams(dp(8)));

        nativeManualAddressButton = actionButton("Enter server address", false);
        nativeManualAddressButton.setOnClickListener(v -> promptForAddress());
        panel.addView(nativeManualAddressButton, buttonParams(dp(8)));

        useLastButton = actionButton("Use last successful address", false);
        useLastButton.setOnClickListener(v -> {
            String last = preferences.getString(PREF_LAST_SUCCESS, "");
            if (!last.isEmpty()) attemptBase(last, false);
        });
        panel.addView(useLastButton, buttonParams(dp(8)));

        Button help = actionButton("Help & setup", false);
        help.setOnClickListener(v -> showHelpSetup());
        panel.addView(help, buttonParams(dp(8)));
        return panel;
    }

    private void renderDiscoveredServers(List<NativeConnectionController.ServerCandidate> servers) {
        if (nativeServerList == null) return;
        nativeServerList.removeAllViews();
        if (servers == null || servers.isEmpty()) {
            TextView empty = new TextView(this);
            empty.setText("No ranker has been discovered yet. You can still use the saved or manual address.");
            empty.setTextSize(12f);
            empty.setTextColor(Color.rgb(154, 168, 188));
            empty.setGravity(Gravity.CENTER);
            empty.setPadding(dp(8), dp(8), dp(8), dp(8));
            nativeServerList.addView(empty, matchWrapParams());
            return;
        }
        int shown = 0;
        for (NativeConnectionController.ServerCandidate server : servers) {
            if (server == null || server.baseUrl == null || server.baseUrl.isEmpty()) continue;
            if (shown++ >= 5) break;
            Button button = actionButton(server.name + "\n" + server.baseUrl, false);
            button.setTextSize(13f);
            button.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
            button.setPadding(dp(12), 0, dp(12), 0);
            button.setContentDescription(server.detail);
            button.setOnClickListener(v -> {
                selectedPairingBase = server.baseUrl;
                nativeSelectedServer.setText("Selected server: " + server.name + "\n" + server.baseUrl);
                attemptBase(server.baseUrl, false);
            });
            nativeServerList.addView(button, buttonParams(shown == 1 ? 0 : dp(6)));
        }
    }

    private void showNativePairing(String baseUrl, String serverName, String message) {
        handler.removeCallbacks(reconnectRunnable);
        String normalized;
        try {
            normalized = normalizeBase(baseUrl);
        } catch (Exception ignored) {
            normalized = DEFAULT_BASE;
        }
        selectedPairingBase = normalized;
        attemptedBase = normalized;
        if (connectionController != null) {
            connectionController.addManualCandidate(normalized,
                    serverName == null || serverName.isEmpty() ? "Artist Ranker" : serverName,
                    "Selected server");
            connectionController.startDiscovery();
        }
        connectionTitle.setText("Pair this Android app");
        connectionMessage.setText(message == null || message.trim().isEmpty()
                ? "Enter the six-character code shown on the ranker PC."
                : message);
        connectionProgress.setVisibility(View.GONE);
        nativeSelectedServer.setText("Selected server: "
                + (serverName == null || serverName.isEmpty() ? "Artist Ranker" : serverName)
                + "\n" + normalized);
        nativePairingControls.setVisibility(View.VISIBLE);
        connectionScroll.setVisibility(View.VISIBLE);
        webView.setVisibility(View.INVISIBLE);
        menuButton.setVisibility(View.GONE);
        updateLastSuccessfulButton();
        maybeStartNativePairingTour();
    }

    private void forgetCurrentPairing() {
        String base = attemptedBase == null ? DEFAULT_BASE : attemptedBase;
        new AlertDialog.Builder(this)
                .setTitle("Forget this paired device?")
                .setMessage("The PC will revoke this Android app's current pairing cookie. A new one-use code will be required.")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Forget", (dialog, which) -> {
                    if (connectionController != null) connectionController.forget(base);
                })
                .show();
    }

    private void showHelpSetup() {
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(8), 0, dp(8), 0);

        TextView message = new TextView(this);
        message.setText(
                "Quick start\n" +
                "1. Start the ranker on the PC.\n" +
                "2. Let the app discover the PC or enter its private LAN address.\n" +
                "3. Create a one-use code under Storage / Settings → Phone pairing and LAN security.\n" +
                "4. Enter the code here; the NovelAI API key never leaves the PC.\n\n" +
                "Duel controls\n" +
                "• Swipe horizontally or tap an image to choose it.\n" +
                "• Two fingers down: Tie.\n" +
                "• Three fingers down: Both bad.\n" +
                "• Three fingers up: Undo.\n" +
                "• Long-press an image for image and artist actions.\n" +
                "• Use the bottom ribbon for Gallery, Generation, Duel, and Artist Ladder.\n\n" +
                "Backup and recovery\n" +
                "Storage / Settings contains exports, import previews, restore points, integrity checks, and safe updates.");
        message.setTextSize(14f);
        message.setTextColor(Color.rgb(225, 232, 243));
        message.setLineSpacing(0f, 1.12f);
        content.addView(message, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        CheckBox showHelp = new CheckBox(this);
        showHelp.setText("Show floating Help button");
        showHelp.setTextColor(Color.rgb(238, 243, 251));
        showHelp.setChecked(preferences.getBoolean(PREF_SHOW_HELP, true));
        showHelp.setPadding(0, dp(12), 0, dp(4));
        showHelp.setOnCheckedChangeListener((button, checked) -> {
            preferences.edit().putBoolean(PREF_SHOW_HELP, checked).apply();
            syncPagePreferences();
        });
        content.addView(showHelp, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        new AlertDialog.Builder(this)
                .setTitle("Artist Ranker Help & Setup")
                .setView(content)
                .setNegativeButton("Close", null)
                .setNeutralButton("Connection", (dialog, which) -> showNativePairing(
                        attemptedBase == null ? DEFAULT_BASE : attemptedBase,
                        "Artist Ranker", "Choose a server or pair this Android app."))
                .setPositiveButton("Restart tour", (dialog, which) -> restartNativeTour())
                .show();
    }

    private void maybeStartNativePairingTour() {
        if (nativeGuideOverlay == null
                || preferences.getInt(PREF_NATIVE_PAIRING_TOUR, 0) >= NATIVE_TOUR_VERSION) return;
        handler.postDelayed(() -> {
            if (connectionScroll.getVisibility() != View.VISIBLE
                    || nativePairingControls.getVisibility() != View.VISIBLE
                    || nativeGuideOverlay.isShowingTour()) return;
            java.util.ArrayList<NativeGuideOverlay.Step> steps = new java.util.ArrayList<>();
            steps.add(new NativeGuideOverlay.Step(connectionTitle,
                    "Native connection and pairing",
                    "The Android app finds the PC before opening the ranker. A QR link can open this screen and fill the one-use pairing code automatically."));
            steps.add(new NativeGuideOverlay.Step(nativeServerList,
                    "Detected Artist Ranker servers",
                    "Tap a discovered PC, use the remembered address, or enter a private LAN address manually."));
            steps.add(new NativeGuideOverlay.Step(nativePairingCode,
                    "Enter the one-use code",
                    "Create a six-character code on the PC. It expires after ten minutes and works once."));
            steps.add(new NativeGuideOverlay.Step(nativeManualAddressButton,
                    "Manual fallback and Help",
                    "If mDNS discovery is blocked, enter the PC's private IPv4 address. Help & Setup remains available from this screen and the app menu."));
            nativeGuideOverlay.showTour(steps, () ->
                    preferences.edit().putInt(PREF_NATIVE_PAIRING_TOUR, NATIVE_TOUR_VERSION).apply());
        }, 500L);
    }

    private java.util.ArrayList<NativeGuideOverlay.Step> nativeAppTourSteps() {
        java.util.ArrayList<NativeGuideOverlay.Step> steps = new java.util.ArrayList<>();
        steps.add(new NativeGuideOverlay.Step(menuButton,
                "Native app menu",
                "Open Duel or the full ranker, change or forget the paired server, control sound, haptics, notifications, status-bar and Help-button visibility, and restart this tour."));
        steps.add(webTourStep("#stageA", "#stageA",
                "Voting gestures",
                "Swipe horizontally or tap to choose A or B. Two fingers down means Tie, three fingers down means Both bad, and three fingers up means Undo. Reversing direction before release changes the pending choice."));
        steps.add(webTourStep("#toolsButton", "#toolsButton,#stageA",
                "Long-press actions and Duel tools",
                "Long-press either image for image and artist favorites, tags, notes, and reports. The separate Duel tools button above the first image controls the complete duel; redundant image three-dot buttons are removed."));
        steps.add(webTourStep("#navigationRibbon", "#resultCard,#navigationRibbon",
                "Results and the bottom ribbon",
                "After a decision, result details and rating changes appear below the images. Swipe the bottom ribbon to move between Gallery, Generation, Duel, and Artist Ladder."));
        return steps;
    }

    private NativeGuideOverlay.Step webTourStep(String anchorSelector, String highlightSelectors, String title, String body) {
        return new NativeGuideOverlay.Step(
                callback -> locateWebTourTarget(anchorSelector, highlightSelectors, callback),
                title,
                body);
    }

    private void locateWebTourTarget(String anchorSelector, String highlightSelectors, NativeGuideOverlay.TargetCallback callback) {
        if (webView == null || nativeGuideOverlay == null || webView.getVisibility() != View.VISIBLE) {
            callback.onTarget(null);
            return;
        }
        final String quotedAnchor = JSONObject.quote(String.valueOf(anchorSelector == null ? "" : anchorSelector));
        final String quotedHighlights = JSONObject.quote(String.valueOf(highlightSelectors == null ? "" : highlightSelectors));
        final String scrollScript =
                "(function(){"
                + "const anchor=document.querySelector(" + quotedAnchor + ");"
                + "if(!anchor)return false;"
                + "anchor.scrollIntoView({behavior:'smooth',block:'center',inline:'nearest'});"
                + "return true;"
                + "})()";
        webView.evaluateJavascript(scrollScript, ignored ->
                handler.postDelayed(() -> measureWebTourTarget(quotedHighlights, callback), 520L));
    }

    private void measureWebTourTarget(String quotedSelectors, NativeGuideOverlay.TargetCallback callback) {
        if (webView == null || nativeGuideOverlay == null || webView.getVisibility() != View.VISIBLE) {
            callback.onTarget(null);
            return;
        }
        final String measureScript =
                "(function(){"
                + "const nodes=Array.from(document.querySelectorAll(" + quotedSelectors + ")).filter(n=>{const r=n.getBoundingClientRect();return r.width>1&&r.height>1&&r.bottom>0&&r.top<window.innerHeight;});"
                + "if(!nodes.length)return null;"
                + "const rects=nodes.map(n=>n.getBoundingClientRect());"
                + "return [Math.min(...rects.map(r=>r.left)),Math.min(...rects.map(r=>r.top)),"
                + "Math.max(...rects.map(r=>r.right)),Math.max(...rects.map(r=>r.bottom)),"
                + "Math.max(1,window.innerWidth),Math.max(1,window.innerHeight)];"
                + "})()";
        webView.evaluateJavascript(measureScript, value -> {
            try {
                if (value == null || "null".equals(value)) {
                    callback.onTarget(null);
                    return;
                }
                JSONArray values = new JSONArray(value);
                float viewportWidth = (float) values.optDouble(4, 1.0);
                float viewportHeight = (float) values.optDouble(5, 1.0);
                float scaleX = webView.getWidth() / Math.max(1f, viewportWidth);
                float scaleY = webView.getHeight() / Math.max(1f, viewportHeight);
                int[] webLocation = new int[2];
                int[] overlayLocation = new int[2];
                webView.getLocationOnScreen(webLocation);
                nativeGuideOverlay.getLocationOnScreen(overlayLocation);
                Rect rect = new Rect(
                        webLocation[0] - overlayLocation[0] + Math.round((float) values.optDouble(0, 0.0) * scaleX),
                        webLocation[1] - overlayLocation[1] + Math.round((float) values.optDouble(1, 0.0) * scaleY),
                        webLocation[0] - overlayLocation[0] + Math.round((float) values.optDouble(2, viewportWidth) * scaleX),
                        webLocation[1] - overlayLocation[1] + Math.round((float) values.optDouble(3, viewportHeight) * scaleY));
                callback.onTarget(rect);
            } catch (Exception error) {
                callback.onTarget(null);
            }
        });
    }

    private void maybeStartNativeAppTour() {
        if (nativeGuideOverlay == null
                || preferences.getInt(PREF_NATIVE_APP_TOUR, 0) >= NATIVE_TOUR_VERSION) return;
        handler.postDelayed(() -> {
            if (webView.getVisibility() != View.VISIBLE || nativeGuideOverlay.isShowingTour()) return;
            nativeGuideOverlay.showTour(nativeAppTourSteps(), () ->
                    preferences.edit().putInt(PREF_NATIVE_APP_TOUR, NATIVE_TOUR_VERSION).apply());
        }, 650L);
    }

    private void restartNativeTour() {
        preferences.edit()
                .putInt(PREF_NATIVE_PAIRING_TOUR, 0)
                .putInt(PREF_NATIVE_APP_TOUR, 0)
                .apply();
        if (connectionScroll.getVisibility() == View.VISIBLE) maybeStartNativePairingTour();
        else maybeStartNativeAppTour();
    }

    @Override
    public void onDiscoveryState(String message, boolean working) {
        if (nativeDiscoveryStatus != null) {
            nativeDiscoveryStatus.setText(message == null ? "" : message);
            nativeDiscoveryStatus.setTextColor(working
                    ? Color.rgb(112, 168, 255)
                    : Color.rgb(154, 168, 188));
        }
    }

    @Override
    public void onServersChanged(List<NativeConnectionController.ServerCandidate> servers) {
        lastDiscoveredServers = servers == null ? new ArrayList<>() : new ArrayList<>(servers);
        renderDiscoveredServers(lastDiscoveredServers);
        tryAutoDiscoveredServer();
    }

    private void tryAutoDiscoveredServer() {
        if (autoDiscoveredConnectAttempted
                || isLoading
                || connectionScroll == null
                || connectionScroll.getVisibility() != View.VISIBLE
                || lastDiscoveredServers == null
                || lastDiscoveredServers.isEmpty()) return;
        NativeConnectionController.ServerCandidate first = lastDiscoveredServers.get(0);
        if (first == null || first.baseUrl == null || first.baseUrl.isEmpty()
                || first.baseUrl.equals(attemptedBase)) return;
        autoDiscoveredConnectAttempted = true;
        attemptBase(first.baseUrl, true);
    }

    @Override
    public void onConnecting(String baseUrl, String message) {
        selectedPairingBase = baseUrl;
        showConnecting("Connecting to Artist Ranker", message == null ? "Trying " + baseUrl : message);
    }

    @Override
    public void onPairingRequired(String baseUrl, String serverName, String message) {
        showNativePairing(baseUrl, serverName, message);
    }

    @Override
    public void onAuthorized(String baseUrl, String deviceName, JSONObject handshake) {
        attemptedBase = baseUrl;
        selectedPairingBase = baseUrl;
        preferences.edit().putString(PREF_LAST_SUCCESS, baseUrl).apply();
        updateLastSuccessfulButton();
        showConnecting("Opening Artist Ranker",
                "Paired as " + (deviceName == null || deviceName.isEmpty() ? "this Android device" : deviceName)
                        + ". Loading Duel…");
        webView.stopLoading();
        webView.loadUrl(baseUrl + "/duel?source=android-app&native=1");
    }

    @Override
    public void onCompatibilityError(String baseUrl, String message) {
        showNativePairing(baseUrl, "Artist Ranker",
                message == null ? "Update the older app or server before pairing." : message);
    }

    @Override
    public void onConnectionError(String baseUrl, String message) {
        attemptedBase = baseUrl;
        showConnectionFailure(message == null ? "Could not reach the server." : message);
    }

    @Override
    public void onPairingForgotten(String baseUrl) {
        preferences.edit().remove(PREF_LAST_SUCCESS).apply();
        updateLastSuccessfulButton();
        showNativePairing(baseUrl, "Artist Ranker",
                "This Android app was forgotten. Create and enter a new one-use code to pair again.");
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setLoadWithOverviewMode(false);
        settings.setUseWideViewPort(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        settings.setUserAgentString(settings.getUserAgentString() + " ArtistRankerApp/" + BuildConfig.VERSION_NAME);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);
        webView.addJavascriptInterface(new NativeBridge(), "ArtistRankerNative");
        webView.setBackgroundColor(Color.rgb(11, 14, 18));
        webView.setWebChromeClient(new WebChromeClient());
        webView.setDownloadListener((url, userAgent, contentDisposition, mimetype, contentLength) -> openExternal(url));
        webView.setOnTouchListener((view, event) -> {
            if (event.getActionMasked() == MotionEvent.ACTION_DOWN || event.getActionMasked() == MotionEvent.ACTION_UP) {
                keepAwakeForActiveVoting();
            }
            return false;
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return routeUrl(request.getUrl().toString());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return routeUrl(url);
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                isLoading = true;
                mainFrameFailed = false;
                showConnecting("Connecting to Artist Ranker", "Trying " + url);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                isLoading = false;
                if (!mainFrameFailed && isAllowedLocalUrl(url) && !url.startsWith("about:")) {
                    String base = baseFromUrl(url);
                    if (base != null) {
                        attemptedBase = base;
                        preferences.edit().putString(PREF_LAST_SUCCESS, base).apply();
                        updateLastSuccessfulButton();
                    }
                    connectionScroll.setVisibility(View.GONE);
                    webView.setVisibility(View.VISIBLE);
                    menuButton.setVisibility(View.VISIBLE);
                    syncPagePreferences();
                    keepAwakeForActiveVoting();
                    maybeStartNativeAppTour();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    mainFrameFailed = true;
                    isLoading = false;
                    showConnectionFailure(error == null ? "Connection failed" : error.getDescription().toString());
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse errorResponse) {
                if (request.isForMainFrame() && errorResponse != null && errorResponse.getStatusCode() >= 400) {
                    mainFrameFailed = true;
                    isLoading = false;
                    showConnectionFailure("Server returned HTTP " + errorResponse.getStatusCode());
                }
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, android.net.http.SslError error) {
                handler.cancel();
                mainFrameFailed = true;
                isLoading = false;
                showConnectionFailure("The server's HTTPS certificate could not be verified.");
            }

            @Override
            public boolean onRenderProcessGone(WebView view, android.webkit.RenderProcessGoneDetail detail) {
                mainFrameFailed = true;
                isLoading = false;
                showConnectionFailure("Android WebView stopped unexpectedly. Retry to reopen it.");
                return true;
            }
        });
    }

    private boolean routeUrl(String url) {
        if (isAllowedLocalUrl(url)) return false;
        openExternal(url);
        return true;
    }

    private void showAppMenu(View anchor) {
        PopupMenu popup = new PopupMenu(this, anchor);
        popup.getMenu().add(0, MENU_DUEL, 0, "Duel page");
        popup.getMenu().add(0, MENU_RANKER, 1, "Full ranker");
        popup.getMenu().add(0, MENU_RELOAD, 2, "Reload");
        popup.getMenu().add(0, MENU_SERVER, 3, "Connection & pairing");
        popup.getMenu().add(0, MENU_FORGET_PAIRING, 4, "Forget this paired device");
        popup.getMenu().add(0, MENU_SOUND, 10, "Sound effects")
                .setCheckable(true).setChecked(preferences.getBoolean(PREF_SOUND, true));
        popup.getMenu().add(0, MENU_HAPTICS, 11, "Both-bad haptics")
                .setCheckable(true).setChecked(preferences.getBoolean(PREF_HAPTICS, true));
        popup.getMenu().add(0, MENU_NOTIFICATIONS, 12, "Buffer-full notifications")
                .setCheckable(true).setChecked(preferences.getBoolean(PREF_NOTIFICATIONS, true));
        popup.getMenu().add(0, MENU_HIDE_STATUS, 13, "Hide status bar")
                .setCheckable(true).setChecked(preferences.getBoolean(PREF_HIDE_STATUS, true));
        popup.getMenu().add(0, MENU_SHOW_HELP, 14, "Show floating Help button")
                .setCheckable(true).setChecked(preferences.getBoolean(PREF_SHOW_HELP, true));
        popup.getMenu().add(0, MENU_HELP_SETUP, 20, "Help & setup");
        popup.getMenu().add(0, MENU_RESTART_TOUR, 21, "Restart app tour");
        popup.getMenu().add(0, MENU_TROUBLESHOOTING, 22, "Connection troubleshooting");
        popup.setOnMenuItemClickListener(item -> {
            switch (item.getItemId()) {
                case MENU_DUEL:
                    loadLocalPath("/duel?source=android-app");
                    return true;
                case MENU_RANKER:
                    loadLocalPath("/ranker/");
                    return true;
                case MENU_RELOAD:
                    webView.reload();
                    return true;
                case MENU_SERVER:
                    showNativePairing(attemptedBase == null ? DEFAULT_BASE : attemptedBase,
                            "Artist Ranker", "Choose a detected server, enter a new address, or pair again.");
                    return true;
                case MENU_FORGET_PAIRING:
                    forgetCurrentPairing();
                    return true;
                case MENU_SOUND:
                    togglePreference(item, PREF_SOUND, true);
                    syncPagePreferences();
                    return true;
                case MENU_HAPTICS:
                    togglePreference(item, PREF_HAPTICS, true);
                    return true;
                case MENU_NOTIFICATIONS:
                    boolean notifications = togglePreference(item, PREF_NOTIFICATIONS, true);
                    if (notifications) requestNotificationPermissionIfNeeded();
                    else {
                        BufferMonitorSupport.stopMonitoring(this);
                        BufferMonitorSupport.cancelReadyNotification(this);
                    }
                    return true;
                case MENU_HIDE_STATUS:
                    togglePreference(item, PREF_HIDE_STATUS, true);
                    applyStatusBarPreference();
                    return true;
                case MENU_SHOW_HELP:
                    togglePreference(item, PREF_SHOW_HELP, true);
                    syncPagePreferences();
                    return true;
                case MENU_HELP_SETUP:
                    showHelpSetup();
                    return true;
                case MENU_RESTART_TOUR:
                    restartNativeTour();
                    return true;
                case MENU_TROUBLESHOOTING:
                    showTroubleshooting();
                    return true;
                default:
                    return false;
            }
        });
        popup.show();
    }

    private boolean togglePreference(MenuItem item, String key, boolean fallback) {
        boolean next = !preferences.getBoolean(key, fallback);
        preferences.edit().putBoolean(key, next).apply();
        item.setChecked(next);
        return next;
    }

    private void loadLocalPath(String path) {
        String base = attemptedBase == null ? DEFAULT_BASE : attemptedBase;
        webView.loadUrl(base + path);
    }

    private void ensurePreferenceDefaults() {
        SharedPreferences.Editor editor = preferences.edit();
        if (!preferences.contains(PREF_SOUND)) editor.putBoolean(PREF_SOUND, true);
        if (!preferences.contains(PREF_HAPTICS)) editor.putBoolean(PREF_HAPTICS, true);
        if (!preferences.contains(PREF_NOTIFICATIONS)) editor.putBoolean(PREF_NOTIFICATIONS, true);
        if (!preferences.contains(PREF_HIDE_STATUS)) editor.putBoolean(PREF_HIDE_STATUS, true);
        if (!preferences.contains(PREF_SHOW_HELP)) editor.putBoolean(PREF_SHOW_HELP, true);
        editor.apply();
    }

    private void syncPagePreferences() {
        if (webView == null) return;
        boolean sound = preferences.getBoolean(PREF_SOUND, true);
        boolean showHelp = preferences.getBoolean(PREF_SHOW_HELP, true);
        String script = "(function(){try{" +
                "localStorage.setItem('artistElo.soundsEnabled','" + (sound ? "true" : "false") + "');" +
                "localStorage.setItem('artistRanker.guidance.helpButtonVisible','" + (showHelp ? "true" : "false") + "');" +
                "localStorage.setItem('artistRanker.duelGuidance.helpButtonVisible','" + (showHelp ? "true" : "false") + "');" +
                "window.ArtistRankerGuidance?.setHelpButtonVisible?.(" + (showHelp ? "true" : "false") + ",{native:false});" +
                "window.ArtistRankerDuelGuidance?.setHelpButtonVisible?.(" + (showHelp ? "true" : "false") + ",{native:false});" +
                "window.dispatchEvent(new CustomEvent('artist-ranker-native-settings',{detail:{soundEnabled:" +
                (sound ? "true" : "false") + ",helpButtonVisible:" + (showHelp ? "true" : "false") + "}}));" +
                "}catch(_){}})()";
        webView.evaluateJavascript(script, null);
    }

    private void performBothBadHaptic() {
        if (!preferences.getBoolean(PREF_HAPTICS, true) || webView == null) return;
        Vibrator vibrator = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
        if (vibrator != null && vibrator.hasVibrator()) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                long[] timings = new long[]{0L, 32L, 42L, 38L};
                int[] amplitudes = new int[]{0, 170, 0, 225};
                vibrator.vibrate(VibrationEffect.createWaveform(timings, amplitudes, -1));
            } else {
                vibrator.vibrate(new long[]{0L, 35L, 45L, 40L}, -1);
            }
            return;
        }
        int constant = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
                ? HapticFeedbackConstants.REJECT
                : HapticFeedbackConstants.VIRTUAL_KEY;
        webView.performHapticFeedback(constant);
    }

    private void applyStatusBarPreference() {
        boolean hide = preferences != null && preferences.getBoolean(PREF_HIDE_STATUS, true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            WindowInsetsController controller = getWindow().getInsetsController();
            if (controller != null) {
                controller.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                if (hide) controller.hide(WindowInsets.Type.statusBars());
                else controller.show(WindowInsets.Type.statusBars());
            }
        } else {
            int flags = View.SYSTEM_UI_FLAG_LAYOUT_STABLE;
            if (hide) {
                flags |= View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY;
            }
            getWindow().getDecorView().setSystemUiVisibility(flags);
        }
    }

    private void requestNotificationPermissionIfNeeded() {
        if (!preferences.getBoolean(PREF_NOTIFICATIONS, true)) return;
        BufferMonitorSupport.ensureChannels(this);
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQUEST_NOTIFICATIONS);
        }
    }

    private void handleBufferState(int ready, int target) {
        BufferMonitorSupport.recordBufferState(this, ready, target);
        if (appInForeground) BufferMonitorSupport.cancelReadyNotification(this);
    }

    private void attemptBase(String rawBase, boolean automatic) {
        String normalized;
        try {
            normalized = normalizeBase(rawBase);
        } catch (IllegalArgumentException error) {
            showConnectionFailure(error.getMessage());
            return;
        }
        attemptedBase = normalized;
        mainFrameFailed = false;
        isLoading = true;
        handler.removeCallbacks(reconnectRunnable);
        showConnecting(automatic ? "Reconnecting to Artist Ranker" : "Connecting to Artist Ranker", "Trying " + normalized);
        selectedPairingBase = normalized;
        if (connectionController == null) {
            showConnectionFailure("Native connection controller is not ready yet.");
            return;
        }
        connectionController.addManualCandidate(normalized, "Artist Ranker", "Saved or manually entered server");
        connectionController.connect(normalized);
    }

    private void showConnecting(String title, String message) {
        connectionTitle.setText(title);
        connectionMessage.setText(message);
        connectionProgress.setVisibility(View.VISIBLE);
        if (nativePairingControls != null) nativePairingControls.setVisibility(View.GONE);
        connectionScroll.setVisibility(View.VISIBLE);
        webView.setVisibility(View.INVISIBLE);
        menuButton.setVisibility(View.GONE);
    }

    private void showConnectionFailure(String technicalMessage) {
        isLoading = false;
        handler.removeCallbacks(reconnectRunnable);
        handler.postDelayed(this::tryAutoDiscoveredServer, 80L);
        String server = attemptedBase == null ? DEFAULT_BASE : attemptedBase;
        boolean android17OrNewer = Build.VERSION.SDK_INT >= 37;
        connectionTitle.setText("Artist Ranker is offline");
        String android17Note = android17OrNewer
                ? "\n\nAndroid 17 note: version " + BuildConfig.VERSION_NAME + " targets Android 16, so the new Local Network permission should not be required yet. " +
                  "A future app version that targets Android 17 must request ACCESS_LOCAL_NETWORK; if that future version reports permission blocking, " +
                  "update Artist Ranker and allow Local Network access in Android Settings."
                : "";
        connectionMessage.setText(
                "The app could not reach your PC. Confirm that the ranker is running and both devices are on the same network.\n\n" +
                "Server attempted: " + server + "\n\n" + technicalMessage + android17Note);
        connectionProgress.setVisibility(View.GONE);
        if (nativePairingControls != null) nativePairingControls.setVisibility(View.VISIBLE);
        connectionScroll.setVisibility(View.VISIBLE);
        webView.setVisibility(View.INVISIBLE);
        menuButton.setVisibility(View.GONE);
        if (isNetworkAvailable()) handler.postDelayed(reconnectRunnable, 5000L);
    }

    private void promptForAddress() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("http://192.168.1.25:7860");
        input.setText(attemptedBase == null ? DEFAULT_BASE : attemptedBase);
        input.setSelectAllOnFocus(true);
        int padding = dp(18);
        FrameLayout holder = new FrameLayout(this);
        holder.setPadding(padding, 0, padding, 0);
        holder.addView(input, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        new AlertDialog.Builder(this)
                .setTitle("Server address")
                .setMessage("Use a discovered .local hostname, the PC hostname, localhost for emulator testing, or a private LAN IPv4 address.")
                .setView(holder)
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Connect", (dialog, which) -> attemptBase(input.getText().toString(), false))
                .show();
    }

    private void showTroubleshooting() {
        String message =
                "1. Start Artist Ranker on the PC.\n\n" +
                "2. Keep the phone and PC on the same Wi-Fi or LAN. Guest Wi-Fi can block device-to-device traffic.\n\n" +
                "3. Allow Python through Windows Firewall for Private networks.\n\n" +
                "4. Try the PC's current IPv4 address, for example http://192.168.x.x:7860.\n\n" +
                "5. The app intentionally refuses public internet addresses; external sites open in your browser.\n\n" +
                "Android 17+: this v" + BuildConfig.VERSION_NAME + " build still targets Android 16. A later build that targets Android 17 must request Local Network access; allow it in Android Settings when that update is released.";
        new AlertDialog.Builder(this)
                .setTitle("Connection help")
                .setMessage(message)
                .setNegativeButton("Close", null)
                .setPositiveButton("App settings", (dialog, which) -> {
                    Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                })
                .show();
    }

    private void openExternal(String rawUrl) {
        try {
            Uri uri = Uri.parse(rawUrl);
            String scheme = uri.getScheme();
            if (scheme == null || !(scheme.equalsIgnoreCase("http") || scheme.equalsIgnoreCase("https"))) return;
            Intent intent = new Intent(Intent.ACTION_VIEW, uri);
            startActivity(intent);
        } catch (ActivityNotFoundException ignored) {
            new AlertDialog.Builder(this)
                    .setTitle("No browser available")
                    .setMessage("Android could not find an app to open this link.")
                    .setPositiveButton("OK", null)
                    .show();
        }
    }

    private String normalizeBase(String rawValue) {
        String value = rawValue == null ? "" : rawValue.trim();
        if (value.isEmpty()) throw new IllegalArgumentException("Enter a server address.");
        if (!value.contains("://")) value = "http://" + value;
        try {
            URI uri = new URI(value);
            String scheme = uri.getScheme() == null ? "http" : uri.getScheme().toLowerCase(Locale.ROOT);
            if (!scheme.equals("http") && !scheme.equals("https")) {
                throw new IllegalArgumentException("Only HTTP or HTTPS server addresses are supported.");
            }
            String host = uri.getHost();
            if (host == null || host.isEmpty()) throw new IllegalArgumentException("The server address has no valid host.");
            if (!isAllowedLocalHost(host)) {
                throw new IllegalArgumentException("For safety, Artist Ranker only connects to local hostnames or private LAN addresses.");
            }
            int port = uri.getPort();
            if (port < 0) port = 7860;
            return scheme + "://" + host + ":" + port;
        } catch (URISyntaxException error) {
            throw new IllegalArgumentException("The server address is not valid.");
        }
    }

    private boolean isAllowedLocalUrl(String rawUrl) {
        try {
            URI uri = new URI(rawUrl);
            String scheme = uri.getScheme();
            return scheme != null && (scheme.equalsIgnoreCase("http") || scheme.equalsIgnoreCase("https"))
                    && isAllowedLocalHost(uri.getHost());
        } catch (Exception ignored) {
            return false;
        }
    }

    private boolean isAllowedLocalHost(String hostValue) {
        if (hostValue == null) return false;
        String host = hostValue.trim().toLowerCase(Locale.ROOT);
        if (host.equals("localhost") || host.equals("127.0.0.1")) {
            return true;
        }
        if (host.endsWith(".local")) {
            String localName = host.substring(0, host.length() - 6);
            return !localName.isEmpty() && localName.matches("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?");
        }
        if (!host.contains(".")) {
            return host.matches("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?");
        }
        String[] parts = host.split("\\.");
        if (parts.length != 4) return false;
        int[] octets = new int[4];
        try {
            for (int i = 0; i < 4; i++) {
                octets[i] = Integer.parseInt(parts[i]);
                if (octets[i] < 0 || octets[i] > 255) return false;
            }
        } catch (NumberFormatException ignored) {
            return false;
        }
        return octets[0] == 10
                || (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31)
                || (octets[0] == 192 && octets[1] == 168);
    }

    private String baseFromUrl(String rawUrl) {
        try {
            URI uri = new URI(rawUrl);
            if (!isAllowedLocalHost(uri.getHost())) return null;
            int port = uri.getPort();
            if (port < 0) port = uri.getScheme().equalsIgnoreCase("https") ? 443 : 80;
            return uri.getScheme() + "://" + uri.getHost() + ":" + port;
        } catch (Exception ignored) {
            return null;
        }
    }

    private void registerNetworkCallback() {
        connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (connectivityManager == null) return;
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                handler.removeCallbacks(reconnectRunnable);
                if (connectionScroll.getVisibility() == View.VISIBLE && !isLoading) {
                    handler.postDelayed(reconnectRunnable, 800L);
                }
            }

            @Override
            public void onLost(Network network) {
                if (!isNetworkAvailable()) {
                    handler.post(() -> showConnectionFailure("The phone is not connected to a network."));
                }
            }
        };
        try {
            connectivityManager.registerDefaultNetworkCallback(networkCallback);
        } catch (RuntimeException ignored) {
            networkCallback = null;
        }
    }

    private boolean isNetworkAvailable() {
        if (connectivityManager == null) return false;
        Network network = connectivityManager.getActiveNetwork();
        NetworkCapabilities capabilities = connectivityManager.getNetworkCapabilities(network);
        return capabilities != null && (
                capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                        || capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
                        || capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)
                        || capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET));
    }

    private void keepAwakeForActiveVoting() {
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        handler.removeCallbacks(clearKeepAwake);
        handler.postDelayed(clearKeepAwake, KEEP_AWAKE_AFTER_ACTIVITY_MS);
    }

    private void updateLastSuccessfulButton() {
        String last = preferences.getString(PREF_LAST_SUCCESS, "");
        useLastButton.setEnabled(!last.isEmpty());
        useLastButton.setAlpha(last.isEmpty() ? 0.45f : 1f);
        useLastButton.setText(last.isEmpty() ? "No successful address saved" : "Use last successful address");
    }

    private Button actionButton(String text, boolean primary) {
        Button button = new Button(this);
        button.setText(text);
        button.setTextSize(15f);
        button.setTextColor(Color.rgb(238, 243, 251));
        button.setAllCaps(false);
        button.setGravity(Gravity.CENTER);
        int fill = primary ? Color.rgb(42, 83, 142) : Color.rgb(28, 35, 48);
        int stroke = primary ? Color.rgb(112, 168, 255) : Color.rgb(58, 69, 87);
        button.setBackground(roundRect(fill, 14, stroke, dp(1)));
        return button;
    }

    private LinearLayout.LayoutParams buttonParams(int topMargin) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(50));
        params.setMargins(0, topMargin, 0, 0);
        return params;
    }

    private LinearLayout.LayoutParams wrapParams() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private LinearLayout.LayoutParams matchWrapParams() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private GradientDrawable roundRect(int fill, int radiusDp) {
        return roundRect(fill, radiusDp, Color.TRANSPARENT, 0);
    }

    private GradientDrawable roundRect(int fill, int radiusDp, int stroke, int strokeWidth) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(radiusDp));
        if (strokeWidth > 0) drawable.setStroke(strokeWidth, stroke);
        return drawable;
    }

    public static GradientDrawable roundRectStatic(int fill, int radiusPx, int stroke, int strokeWidth) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(radiusPx);
        if (strokeWidth > 0) drawable.setStroke(strokeWidth, stroke);
        return drawable;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void continueBackNavigation() {
        if (connectionScroll.getVisibility() == View.VISIBLE && webView.getUrl() != null && !mainFrameFailed) {
            connectionScroll.setVisibility(View.GONE);
            webView.setVisibility(View.VISIBLE);
            menuButton.setVisibility(View.VISIBLE);
            return;
        }
        if (webView.canGoBack()) {
            webView.goBack();
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Exit Artist Ranker?")
                .setMessage("Your current duel remains queued on the PC.")
                .setNegativeButton("Stay", null)
                .setPositiveButton("Exit", (dialog, which) -> finish())
                .show();
    }

    @Override
    public void onBackPressed() {
        if (webView == null || webView.getVisibility() != View.VISIBLE) {
            continueBackNavigation();
            return;
        }
        String closeTransientUi = "(function(){" +
                "var close=document.querySelector('#sheet.open #sheetClose, #elo-note-modal.is-open .note-close, #elo-favorite-modal.is-open .favorite-close, #gallery-duel-modal.is-open .gallery-duel-close, #artist-portrait-modal.is-open .portrait-close, #artist-head-to-head-modal.is-open .head-to-head-close');" +
                "if(close){close.click();return true;}" +
                "var menu=document.querySelector('#elo-context-menu.is-open');" +
                "if(menu){menu.classList.remove('is-open');menu.setAttribute('aria-hidden','true');return true;}" +
                "return false;})()";
        webView.evaluateJavascript(closeTransientUi, result -> {
            if (!"true".equals(result)) continueBackNavigation();
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        appInForeground = true;
        BufferMonitorSupport.setAppForeground(this, true);
        BufferMonitorSupport.stopMonitoring(this);
        BufferMonitorSupport.cancelReadyNotification(this);
        applyStatusBarPreference();
        syncPagePreferences();
    }

    @Override
    protected void onPause() {
        appInForeground = false;
        BufferMonitorSupport.setAppForeground(this, false);
        if (preferences.getBoolean(PREF_NOTIFICATIONS, true)) {
            BufferMonitorSupport.startMonitoring(this);
        }
        super.onPause();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) applyStatusBarPreference();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (connectionController != null) connectionController.shutdown();
        if (connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (RuntimeException ignored) {
                // Already unregistered.
            }
        }
        if (webView != null) {
            webView.stopLoading();
            webView.setWebChromeClient(null);
            webView.setWebViewClient(null);
            webView.destroy();
        }
        super.onDestroy();
    }
}
