package com.fudgeamin.iptvlibrary;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.system.Os;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Runs the same iptv_library.py the NAS uses (via embedded Python) on 127.0.0.1:8765
 * and shows it full-screen in a WebView.
 */
public class MainActivity extends Activity {
    static final int PORT = 8765;
    static final String URL = "http://127.0.0.1:" + PORT + "/";
    static volatile boolean serverStarted = false;
    static volatile String serverError = null;

    private WebView web;
    private FrameLayout root;
    private View fullView;
    private WebChromeClient.CustomViewCallback fullCallback;

    static File downloadsDir() {
        return new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "IPTV Library");
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0E0F13"));
        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);                       // keeps your IPTV login between launches
        ws.setDatabaseEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        ws.setSupportMultipleWindows(false);
        ws.setUseWideViewPort(true);
        ws.setLoadWithOverviewMode(true);
        web.setBackgroundColor(Color.parseColor("#0E0F13"));
        web.addJavascriptInterface(new Bridge(), "AndroidApp");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if ("127.0.0.1".equals(u.getHost()) || "localhost".equals(u.getHost()) || "about".equals(u.getScheme())) return false;
                openExternal(u, null);                        // GitHub, APK downloads etc. open in the browser
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onShowCustomView(View view, CustomViewCallback callback) {   // full-screen video
                if (fullView != null) { callback.onCustomViewHidden(); return; }
                fullView = view; fullCallback = callback;
                root.addView(view, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                web.setVisibility(View.GONE);
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
                immersive(true);
            }

            @Override
            public void onHideCustomView() {
                if (fullView == null) return;
                root.removeView(fullView); fullView = null;
                web.setVisibility(View.VISIBLE);
                if (fullCallback != null) fullCallback.onCustomViewHidden();
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
                immersive(false);
            }
        });

        web.loadDataWithBaseURL(null, page("Starting IPTV Library…", ""), "text/html", "utf-8", null);
        askPermissions();
        startServer();
        try {
            Intent svc = new Intent(this, KeepAliveService.class);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc); else startService(svc);
        } catch (Exception e) { Log.w("IPTVLibrary", "keep-alive service", e); }
        waitAndLoad();
    }

    private void immersive(boolean on) {
        View d = getWindow().getDecorView();
        if (on) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            d.setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            d.setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE);
        }
    }

    private void askPermissions() {
        if (Build.VERSION.SDK_INT < 23) return;
        List<String> want = new ArrayList<>();
        if (Build.VERSION.SDK_INT <= 28 && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED)
            want.add(Manifest.permission.WRITE_EXTERNAL_STORAGE);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            want.add(Manifest.permission.POST_NOTIFICATIONS);
        if (!want.isEmpty()) requestPermissions(want.toArray(new String[0]), 1);
    }

    private void startServer() {
        if (serverStarted) return;
        serverStarted = true;
        File dl = downloadsDir();
        //noinspection ResultOfMethodCallIgnored
        dl.mkdirs();
        try {
            Os.setenv("IPTV_ANDROID", "1", true);
            Os.setenv("IPTV_CONFIG_DIR", getFilesDir().getAbsolutePath(), true);
            Os.setenv("IPTV_DOWNLOADS", dl.getAbsolutePath(), true);
            Os.setenv("IPTV_BROWSE_ROOT", dl.getParentFile().getAbsolutePath(), true);
            Os.setenv("HOME", getFilesDir().getAbsolutePath(), true);
        } catch (Exception e) { Log.e("IPTVLibrary", "setenv", e); }
        if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
        Thread t = new Thread(() -> {
            try {
                Python.getInstance().getModule("iptv_library").callAttr("main_android", PORT);
            } catch (Throwable e) {
                Log.e("IPTVLibrary", "server stopped", e);
                serverError = String.valueOf(e);
            }
        }, "iptv-server");
        t.setDaemon(true);
        t.start();
    }

    private void waitAndLoad() {
        new Thread(() -> {
            for (int i = 0; i < 300 && serverError == null; i++) {        // up to ~30 s on a slow first start
                try (Socket s = new Socket()) {
                    s.connect(new InetSocketAddress("127.0.0.1", PORT), 300);
                    runOnUiThread(() -> web.loadUrl(URL));
                    return;
                } catch (Exception ignored) {
                    try { Thread.sleep(100); } catch (InterruptedException e) { return; }
                }
            }
            String err = serverError != null ? serverError : "The app didn't start in time.";
            runOnUiThread(() -> web.loadDataWithBaseURL(null, page("IPTV Library couldn't start", err), "text/html", "utf-8", null));
        }).start();
    }

    private static String page(String title, String detail) {
        String esc = detail.replace("&", "&amp;").replace("<", "&lt;");
        return "<html><body style='background:#0E0F13;color:#E9EAEE;font-family:sans-serif;display:flex;align-items:center;"
                + "justify-content:center;height:100vh;margin:0;text-align:center;padding:24px;box-sizing:border-box'>"
                + "<div><h2 style='font-weight:600'>" + title + "</h2><p style='color:#9097A6;word-break:break-word'>" + esc + "</p></div></body></html>";
    }

    private void openExternal(Uri uri, String mime) {
        Intent i = new Intent(Intent.ACTION_VIEW);
        if (mime != null) i.setDataAndType(uri, mime); else i.setData(uri);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivity(mime != null ? Intent.createChooser(i, "Open with").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) : i);
        } catch (ActivityNotFoundException e) {
            runOnUiThread(() -> Toast.makeText(this, "No app found to open this. Install VLC or MX Player.", Toast.LENGTH_LONG).show());
        }
    }

    @Override
    public void onBackPressed() {
        if (fullView != null) { if (fullCallback != null) fullCallback.onCustomViewHidden(); return; }
        web.evaluateJavascript("window.iptvBack ? iptvBack() : false", v -> {
            if (!"true".equals(v)) moveTaskToBack(true);          // keep running (and downloading) in the background
        });
    }

    /** Things the page can ask the phone to do. */
    class Bridge {
        @JavascriptInterface
        public void playExternal(String url, String title) {
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(Uri.parse(url), "video/*");
            i.putExtra("title", title);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                startActivity(Intent.createChooser(i, "Play with").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (ActivityNotFoundException e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Install VLC or MX Player to play this outside the app.", Toast.LENGTH_LONG).show());
            }
        }

        @JavascriptInterface
        public void openUrl(String url) { openExternal(Uri.parse(url), null); }

        @JavascriptInterface
        public void copy(String text) {
            runOnUiThread(() -> {
                ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("IPTV Library", text));
            });
        }

        @JavascriptInterface
        public String saveText(String name, String text) {
            try {
                File dir = new File(downloadsDir(), "Exports");
                //noinspection ResultOfMethodCallIgnored
                dir.mkdirs();
                File f = new File(dir, name);
                try (FileOutputStream out = new FileOutputStream(f)) { out.write(text.getBytes(StandardCharsets.UTF_8)); }
                return "Download/IPTV Library/Exports/" + name;
            } catch (Exception e) {
                Log.e("IPTVLibrary", "save", e);
                return "";
            }
        }
    }
}
