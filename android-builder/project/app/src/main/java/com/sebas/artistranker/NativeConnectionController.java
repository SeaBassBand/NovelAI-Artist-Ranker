package com.sebas.artistranker;

import android.content.Context;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.Inet4Address;
import java.net.NetworkInterface;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.Set;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/** Native Phase 5 refinement: mDNS discovery, protocol checks, pairing, and cookie handoff. */
public final class NativeConnectionController {
    public static final int CLIENT_PROTOCOL = 1;
    public static final String SERVICE_TYPE = "_artist-ranker._tcp.";

    public static final class ServerCandidate {
        public final String baseUrl;
        public final String name;
        public final String detail;

        ServerCandidate(String baseUrl, String name, String detail) {
            this.baseUrl = baseUrl;
            this.name = name;
            this.detail = detail;
        }
    }

    public interface Callback {
        void onDiscoveryState(String message, boolean working);
        void onServersChanged(List<ServerCandidate> servers);
        void onConnecting(String baseUrl, String message);
        void onPairingRequired(String baseUrl, String serverName, String message);
        void onAuthorized(String baseUrl, String deviceName, JSONObject handshake);
        void onCompatibilityError(String baseUrl, String message);
        void onConnectionError(String baseUrl, String message);
        void onPairingForgotten(String baseUrl);
    }

    private final Context context;
    private final Callback callback;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final ExecutorService subnetExecutor = Executors.newFixedThreadPool(12);
    private final Map<String, ServerCandidate> servers = new LinkedHashMap<>();
    private final AtomicBoolean subnetProbeRunning = new AtomicBoolean(false);
    private final NsdManager nsdManager;
    private NsdManager.DiscoveryListener discoveryListener;
    private boolean discovering;

    public NativeConnectionController(Context context, Callback callback) {
        this.context = context.getApplicationContext();
        this.callback = callback;
        this.nsdManager = (NsdManager) context.getSystemService(Context.NSD_SERVICE);
        CookieManager.getInstance().setAcceptCookie(true);
    }

    public void shutdown() {
        stopDiscovery();
        executor.shutdownNow();
        subnetExecutor.shutdownNow();
    }

    public void startDiscovery() {
        startPrivateSubnetProbe();
        if (nsdManager == null) {
            postDiscovery("Android NSD is unavailable on this device.", false);
            return;
        }
        if (discovering) return;
        discoveryListener = new NsdManager.DiscoveryListener() {
            @Override
            public void onDiscoveryStarted(String serviceType) {
                discovering = true;
                postDiscovery("Searching the local network for Artist Ranker…", true);
            }

            @Override
            public void onServiceFound(NsdServiceInfo serviceInfo) {
                if (serviceInfo == null || serviceInfo.getServiceType() == null
                        || !serviceInfo.getServiceType().toLowerCase(Locale.ROOT)
                        .startsWith("_artist-ranker._tcp")) return;
                resolveService(serviceInfo);
            }

            @Override
            public void onServiceLost(NsdServiceInfo serviceInfo) {
                // Keep the candidate visible. A reconnect attempt gives the authoritative result.
            }

            @Override
            public void onDiscoveryStopped(String serviceType) {
                discovering = false;
                postDiscovery("Local discovery stopped.", false);
            }

            @Override
            public void onStartDiscoveryFailed(String serviceType, int errorCode) {
                discovering = false;
                try { nsdManager.stopServiceDiscovery(this); } catch (Exception ignored) {}
                postDiscovery("Local discovery could not start (" + errorCode + "). Manual address entry still works.", false);
            }

            @Override
            public void onStopDiscoveryFailed(String serviceType, int errorCode) {
                discovering = false;
                postDiscovery("Local discovery stopped with error " + errorCode + ".", false);
            }
        };
        try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener);
        } catch (RuntimeException error) {
            discovering = false;
            postDiscovery("Local discovery is unavailable: " + error.getMessage(), false);
        }
    }

    public void stopDiscovery() {
        if (!discovering || nsdManager == null || discoveryListener == null) return;
        try { nsdManager.stopServiceDiscovery(discoveryListener); } catch (Exception ignored) {}
        discovering = false;
    }

    @SuppressWarnings("deprecation")
    private void resolveService(NsdServiceInfo info) {
        try {
            nsdManager.resolveService(info, new NsdManager.ResolveListener() {
                @Override
                public void onResolveFailed(NsdServiceInfo serviceInfo, int errorCode) {
                    // Android can report an already-active resolver; another discovery callback will retry.
                }

                @Override
                public void onServiceResolved(NsdServiceInfo serviceInfo) {
                    InetAddress host = serviceInfo.getHost();
                    int port = serviceInfo.getPort() > 0 ? serviceInfo.getPort() : 7860;
                    if (host == null) return;
                    String address = host.getHostAddress();
                    if (address == null || address.isEmpty()) return;
                    if (address.contains(":")) address = "[" + address + "]";
                    String base = "http://" + address + ":" + port;
                    addCandidate(new ServerCandidate(
                            base,
                            serviceInfo.getServiceName() == null ? "Artist Ranker" : serviceInfo.getServiceName(),
                            "Discovered through Android NSD / mDNS"));
                }
            });
        } catch (RuntimeException ignored) {
        }
    }

    private void startPrivateSubnetProbe() {
        if (!subnetProbeRunning.compareAndSet(false, true)) return;
        executor.execute(() -> {
            Set<String> prefixes = localIpv4Prefixes();
            if (prefixes.isEmpty()) {
                subnetProbeRunning.set(false);
                return;
            }
            postDiscovery("Searching mDNS and the local Wi-Fi subnet for Artist Ranker…", true);
            AtomicInteger pending = new AtomicInteger(prefixes.size() * 254);
            AtomicBoolean found = new AtomicBoolean(false);
            for (String prefix : prefixes) {
                for (int host = 1; host <= 254; host++) {
                    final String base = "http://" + prefix + host + ":7860";
                    subnetExecutor.execute(() -> {
                        try {
                            HttpResult result = requestWithTimeout(
                                    "GET", base + "/api/pairing/handshake?protocol=" + CLIENT_PROTOCOL,
                                    null, false, 260, 420);
                            if (result.code == 200) {
                                JSONObject handshake = new JSONObject(result.body);
                                if (handshake.has("protocol_current")) {
                                    found.set(true);
                                    String name = handshake.optString("server_name", "Artist Ranker");
                                    addCandidate(new ServerCandidate(base, name,
                                            "Found by private-LAN fallback scan"));
                                }
                            }
                        } catch (Exception ignored) {
                        } finally {
                            if (pending.decrementAndGet() == 0) {
                                subnetProbeRunning.set(false);
                                postDiscovery(found.get()
                                        ? "Artist Ranker found on the local network."
                                        : "No server answered the local scan. Manual address entry remains available.",
                                        false);
                            }
                        }
                    });
                }
            }
        });
    }

    private static Set<String> localIpv4Prefixes() {
        Set<String> prefixes = new HashSet<>();
        try {
            Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
            while (interfaces != null && interfaces.hasMoreElements()) {
                NetworkInterface network = interfaces.nextElement();
                if (!network.isUp() || network.isLoopback()) continue;
                Enumeration<InetAddress> addresses = network.getInetAddresses();
                while (addresses.hasMoreElements()) {
                    InetAddress address = addresses.nextElement();
                    if (!(address instanceof Inet4Address) || address.isLoopbackAddress()
                            || !address.isSiteLocalAddress()) continue;
                    String text = address.getHostAddress();
                    int lastDot = text == null ? -1 : text.lastIndexOf('.');
                    if (lastDot > 0) prefixes.add(text.substring(0, lastDot + 1));
                }
            }
        } catch (Exception ignored) {
        }
        return prefixes;
    }

    public void addManualCandidate(String base, String name, String detail) {
        addCandidate(new ServerCandidate(base, name, detail));
    }

    private void addCandidate(ServerCandidate candidate) {
        synchronized (servers) {
            servers.put(candidate.baseUrl, candidate);
        }
        main.post(() -> callback.onServersChanged(snapshotServers()));
    }

    private List<ServerCandidate> snapshotServers() {
        synchronized (servers) {
            return new ArrayList<>(servers.values());
        }
    }

    public void connect(String baseUrl) {
        final String base = trimBase(baseUrl);
        callback.onConnecting(base, "Checking server compatibility and paired-device status…");
        executor.execute(() -> {
            try {
                HttpResult handshakeResult = request("GET", base + "/api/pairing/handshake?protocol=" + CLIENT_PROTOCOL, null, false);
                if (handshakeResult.code != 200) {
                    throw new IllegalStateException(messageFrom(handshakeResult, "Server handshake failed."));
                }
                JSONObject handshake = new JSONObject(handshakeResult.body);
                if (!handshake.optBoolean("compatible", false)) {
                    String message = handshake.optString("compatibility_message", "App/server update required.");
                    main.post(() -> callback.onCompatibilityError(base, message));
                    return;
                }
                String serverName = handshake.optString("server_name", "Artist Ranker");
                addCandidate(new ServerCandidate(base, serverName,
                        "Server " + handshake.optString("server_version", "?") + " · protocol "
                                + handshake.optInt("protocol_current", CLIENT_PROTOCOL)));
                HttpResult sessionResult = request("GET", base + "/api/pairing/session", null, true);
                if (sessionResult.code == 200) {
                    JSONObject session = new JSONObject(sessionResult.body);
                    if (session.optBoolean("authorized", false)) {
                        String deviceName = session.optString("device_name", "This Android device");
                        main.post(() -> callback.onAuthorized(base, deviceName, handshake));
                        return;
                    }
                }
                String message = sessionResult.code == 401
                        ? messageFrom(sessionResult, "Pair this device with a one-use code from the PC.")
                        : "Enter a one-use code from the ranker PC.";
                main.post(() -> callback.onPairingRequired(base, serverName, message));
            } catch (Exception error) {
                main.post(() -> callback.onConnectionError(base, safeMessage(error, "Could not reach the server.")));
            }
        });
    }

    public void pair(String baseUrl, String code) {
        final String base = trimBase(baseUrl);
        final String normalized = String.valueOf(code == null ? "" : code)
                .toUpperCase(Locale.ROOT).replaceAll("[^A-Z0-9]", "");
        if (normalized.length() != 6) {
            callback.onConnectionError(base, "Enter the six-character one-use code shown on the PC.");
            return;
        }
        callback.onConnecting(base, "Pairing this Android app with the ranker PC…");
        executor.execute(() -> {
            try {
                JSONObject payload = new JSONObject();
                payload.put("code", normalized);
                payload.put("device_name", nativeDeviceName());
                payload.put("protocol", CLIENT_PROTOCOL);
                HttpResult result = request("POST", base + "/api/pairing/exchange", payload.toString(), false);
                if (result.code != 200) {
                    throw new IllegalStateException(messageFrom(result, "Pairing failed."));
                }
                if (result.setCookie != null && !result.setCookie.isEmpty()) {
                    CookieManager cookies = CookieManager.getInstance();
                    cookies.setCookie(base, result.setCookie);
                    cookies.flush();
                }
                JSONObject body = new JSONObject(result.body);
                JSONObject handshake = body.optJSONObject("handshake");
                String deviceName = body.optJSONObject("device") == null
                        ? nativeDeviceName()
                        : body.optJSONObject("device").optString("name", nativeDeviceName());
                main.post(() -> callback.onAuthorized(base, deviceName,
                        handshake == null ? new JSONObject() : handshake));
            } catch (Exception error) {
                main.post(() -> callback.onConnectionError(base, safeMessage(error, "Pairing failed.")));
            }
        });
    }

    public void forget(String baseUrl) {
        final String base = trimBase(baseUrl);
        executor.execute(() -> {
            try { request("POST", base + "/api/pairing/forget", "{}", true); } catch (Exception ignored) {}
            CookieManager manager = CookieManager.getInstance();
            manager.setCookie(base, "artist_ranker_pair=; Max-Age=0; Path=/; SameSite=Strict");
            manager.flush();
            main.post(() -> callback.onPairingForgotten(base));
        });
    }

    private HttpResult request(String method, String url, String body, boolean includeCookies) throws Exception {
        return requestWithTimeout(method, url, body, includeCookies, 4200, 5200);
    }

    private HttpResult requestWithTimeout(String method, String url, String body, boolean includeCookies,
                                          int connectTimeout, int readTimeout) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(connectTimeout);
        connection.setReadTimeout(readTimeout);
        connection.setUseCaches(false);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("X-Artist-Ranker-App", "android-native-" + BuildConfig.VERSION_NAME);
        if (includeCookies) {
            String cookie = CookieManager.getInstance().getCookie(trimBase(url));
            if (cookie != null && !cookie.isEmpty()) connection.setRequestProperty("Cookie", cookie);
        }
        if (body != null) {
            byte[] encoded = body.getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setFixedLengthStreamingMode(encoded.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(encoded);
            }
        }
        int code = connection.getResponseCode();
        InputStream stream = code >= 400 ? connection.getErrorStream() : connection.getInputStream();
        String response = readText(stream);
        String setCookie = connection.getHeaderField("Set-Cookie");
        connection.disconnect();
        return new HttpResult(code, response, setCookie);
    }

    private static String readText(InputStream stream) throws Exception {
        if (stream == null) return "";
        StringBuilder body = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) body.append(line);
        }
        return body.toString();
    }

    private static String messageFrom(HttpResult result, String fallback) {
        try {
            JSONObject body = new JSONObject(result.body);
            String detail = body.optString("detail", body.optString("error", ""));
            if (!detail.isEmpty()) return detail;
        } catch (Exception ignored) {}
        return fallback + " (HTTP " + result.code + ")";
    }

    private static String safeMessage(Exception error, String fallback) {
        String message = error == null ? "" : String.valueOf(error.getMessage());
        return message == null || message.trim().isEmpty() ? fallback : message;
    }

    private static String trimBase(String value) {
        String text = String.valueOf(value == null ? "" : value).trim();
        while (text.endsWith("/")) text = text.substring(0, text.length() - 1);
        return text;
    }

    private static String nativeDeviceName() {
        String manufacturer = Build.MANUFACTURER == null ? "Android" : Build.MANUFACTURER;
        String model = Build.MODEL == null ? "device" : Build.MODEL;
        String value = manufacturer + " " + model;
        return value.length() > 80 ? value.substring(0, 80) : value;
    }

    private void postDiscovery(String message, boolean working) {
        main.post(() -> callback.onDiscoveryState(message, working));
    }

    private static final class HttpResult {
        final int code;
        final String body;
        final String setCookie;

        HttpResult(int code, String body, String setCookie) {
            this.code = code;
            this.body = body == null ? "" : body;
            this.setCookie = setCookie;
        }
    }
}
