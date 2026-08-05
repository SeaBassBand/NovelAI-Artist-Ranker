package com.sebas.artistranker;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Region;
import android.os.Build;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

/** Anchored native first-run tour with a spotlight, arrow, and Next/Back/Skip card. */
public final class NativeGuideOverlay extends FrameLayout {
    public interface TargetCallback {
        void onTarget(Rect target);
    }

    public interface TargetLocator {
        void locate(TargetCallback callback);
    }

    public static final class Step {
        public final View target;
        public final TargetLocator locator;
        public final String title;
        public final String body;

        public Step(View target, String title, String body) {
            this.target = target;
            this.locator = null;
            this.title = title;
            this.body = body;
        }

        public Step(TargetLocator locator, String title, String body) {
            this.target = null;
            this.locator = locator;
            this.title = title;
            this.body = body;
        }
    }

    private final SpotlightView spotlight;
    private final LinearLayout card;
    private final TextView kicker;
    private final TextView title;
    private final TextView body;
    private final Button back;
    private final Button next;
    private final Button skip;
    private final List<Step> steps = new ArrayList<>();
    private int index;
    private int renderGeneration;
    private Runnable completion;

    public NativeGuideOverlay(Context context) {
        super(context);
        setVisibility(GONE);
        setClickable(true);
        setFocusable(true);
        setBackgroundColor(Color.TRANSPARENT);

        spotlight = new SpotlightView(context);
        addView(spotlight, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        card = new LinearLayout(context);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(16), dp(14), dp(16), dp(14));
        card.setBackground(MainActivity.roundRectStatic(
                Color.rgb(17, 24, 39), 14, Color.rgb(112, 168, 255), dp(1)));
        card.setElevation(dp(16));

        kicker = text(11f, Color.rgb(159, 178, 204));
        title = text(18f, Color.rgb(238, 243, 251));
        body = text(14f, Color.rgb(199, 211, 227));
        body.setLineSpacing(0f, 1.15f);
        LinearLayout.LayoutParams titleParams = wrap();
        titleParams.setMargins(0, dp(4), 0, 0);
        LinearLayout.LayoutParams bodyParams = wrap();
        bodyParams.setMargins(0, dp(8), 0, 0);
        card.addView(kicker, wrap());
        card.addView(title, titleParams);
        card.addView(body, bodyParams);

        LinearLayout actions = new LinearLayout(context);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams actionsParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        actionsParams.setMargins(0, dp(12), 0, 0);
        card.addView(actions, actionsParams);

        skip = button("Skip");
        back = button("Back");
        next = button("Next");
        next.setBackground(MainActivity.roundRectStatic(
                Color.rgb(54, 95, 157), 9, Color.rgb(112, 168, 255), dp(1)));
        actions.addView(skip, new LinearLayout.LayoutParams(0, dp(42), 1f));
        LinearLayout.LayoutParams backParams = new LinearLayout.LayoutParams(0, dp(42), 1f);
        backParams.setMargins(dp(7), 0, 0, 0);
        actions.addView(back, backParams);
        LinearLayout.LayoutParams nextParams = new LinearLayout.LayoutParams(0, dp(42), 1f);
        nextParams.setMargins(dp(7), 0, 0, 0);
        actions.addView(next, nextParams);

        FrameLayout.LayoutParams cardParams = new FrameLayout.LayoutParams(
                Math.min(dp(390), getResources().getDisplayMetrics().widthPixels - dp(24)),
                ViewGroup.LayoutParams.WRAP_CONTENT);
        addView(card, cardParams);

        skip.setOnClickListener(v -> finish(true));
        back.setOnClickListener(v -> {
            if (index > 0) {
                index--;
                render();
            }
        });
        next.setOnClickListener(v -> {
            if (index >= steps.size() - 1) finish(true);
            else {
                index++;
                render();
            }
        });
    }

    public void showTour(List<Step> newSteps, Runnable onComplete) {
        steps.clear();
        if (newSteps != null) {
            for (Step step : newSteps) {
                if (step != null && (step.target != null || step.locator != null)) steps.add(step);
            }
        }
        if (steps.isEmpty()) {
            if (onComplete != null) onComplete.run();
            return;
        }
        completion = onComplete;
        index = 0;
        setVisibility(VISIBLE);
        bringToFront();
        post(this::render);
    }

    public boolean isShowingTour() {
        return getVisibility() == VISIBLE;
    }

    public void dismissWithoutCompleting() {
        finish(false);
    }

    private void finish(boolean complete) {
        setVisibility(GONE);
        steps.clear();
        spotlight.setTarget(null);
        Runnable callback = completion;
        completion = null;
        if (complete && callback != null) callback.run();
    }

    private void render() {
        if (steps.isEmpty() || index < 0 || index >= steps.size()) return;
        final Step step = steps.get(index);
        final int generation = ++renderGeneration;

        kicker.setText("APP TOUR · " + (index + 1) + " OF " + steps.size());
        title.setText(step.title);
        body.setText(step.body);
        back.setEnabled(index > 0);
        back.setAlpha(index > 0 ? 1f : 0.45f);
        next.setText(index == steps.size() - 1 ? "Finish" : "Next");

        card.animate().cancel();
        card.setAlpha(0f);
        card.setTranslationY(dp(8));
        spotlight.setTarget(null);

        if (step.locator != null) {
            step.locator.locate(rect -> post(() -> {
                if (generation != renderGeneration || getVisibility() != VISIBLE) return;
                applyTarget(step, rect);
            }));
            return;
        }

        if (step.target == null) {
            applyTarget(step, null);
            return;
        }

        Rect local = new Rect(0, 0, Math.max(1, step.target.getWidth()), Math.max(1, step.target.getHeight()));
        step.target.requestRectangleOnScreen(local, true);
        postDelayed(() -> {
            if (generation != renderGeneration || getVisibility() != VISIBLE) return;
            Rect rect = new Rect();
            int[] overlayLocation = new int[2];
            int[] targetLocation = new int[2];
            getLocationOnScreen(overlayLocation);
            step.target.getLocationOnScreen(targetLocation);
            rect.left = targetLocation[0] - overlayLocation[0];
            rect.top = targetLocation[1] - overlayLocation[1];
            rect.right = rect.left + Math.max(1, step.target.getWidth());
            rect.bottom = rect.top + Math.max(1, step.target.getHeight());
            applyTarget(step, rect);
        }, 230L);
    }

    private void applyTarget(Step step, Rect incoming) {
        Rect rect = incoming == null
                ? new Rect(dp(12), dp(12), Math.max(dp(13), getWidth() - dp(12)), Math.max(dp(13), getHeight() - dp(12)))
                : new Rect(incoming);
        int pad = dp(7);
        rect.inset(-pad, -pad);
        rect.left = Math.max(dp(5), rect.left);
        rect.top = Math.max(dp(5), rect.top);
        rect.right = Math.min(getWidth() - dp(5), Math.max(rect.left + dp(2), rect.right));
        rect.bottom = Math.min(getHeight() - dp(5), Math.max(rect.top + dp(2), rect.bottom));
        spotlight.setTarget(rect);

        card.measure(
                MeasureSpec.makeMeasureSpec(Math.min(dp(390), getWidth() - dp(24)), MeasureSpec.EXACTLY),
                MeasureSpec.makeMeasureSpec(getHeight(), MeasureSpec.AT_MOST));
        int cardWidth = card.getMeasuredWidth();
        int cardHeight = card.getMeasuredHeight();
        boolean below = rect.bottom + dp(18) + cardHeight <= getHeight();
        int left = Math.max(dp(10), Math.min(getWidth() - cardWidth - dp(10),
                rect.centerX() - cardWidth / 2));
        int top = below ? rect.bottom + dp(18) : Math.max(dp(10), rect.top - cardHeight - dp(18));
        FrameLayout.LayoutParams params = (FrameLayout.LayoutParams) card.getLayoutParams();
        params.width = cardWidth;
        params.leftMargin = left;
        params.topMargin = top;
        card.setLayoutParams(params);
        spotlight.setCardPlacement(new Rect(left, top, left + cardWidth, top + cardHeight), below);
        card.animate().alpha(1f).translationY(0f).setDuration(180L).start();
    }

    private TextView text(float size, int color) {
        TextView view = new TextView(getContext());
        view.setTextSize(size);
        view.setTextColor(color);
        return view;
    }

    private Button button(String label) {
        Button button = new Button(getContext());
        button.setText(label);
        button.setTextSize(13f);
        button.setAllCaps(false);
        button.setTextColor(Color.rgb(238, 243, 251));
        button.setPadding(dp(6), 0, dp(6), 0);
        button.setBackground(MainActivity.roundRectStatic(
                Color.rgb(29, 41, 59), 9, Color.rgb(70, 85, 108), dp(1)));
        return button;
    }

    private LinearLayout.LayoutParams wrap() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private static final class SpotlightView extends View {
        private final Paint shade = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint border = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint arrow = new Paint(Paint.ANTI_ALIAS_FLAG);
        private Rect target;
        private Rect card;
        private boolean cardBelow;

        SpotlightView(Context context) {
            super(context);
            setLayerType(Build.VERSION.SDK_INT >= Build.VERSION_CODES.HONEYCOMB
                    ? View.LAYER_TYPE_SOFTWARE : View.LAYER_TYPE_NONE, null);
            shade.setColor(Color.argb(205, 2, 6, 23));
            border.setColor(Color.rgb(112, 168, 255));
            border.setStyle(Paint.Style.STROKE);
            border.setStrokeWidth(dp(3));
            arrow.setColor(Color.rgb(17, 24, 39));
            arrow.setStyle(Paint.Style.FILL);
        }

        void setTarget(Rect value) {
            target = value == null ? null : new Rect(value);
            invalidate();
        }

        void setCardPlacement(Rect value, boolean below) {
            card = value == null ? null : new Rect(value);
            cardBelow = below;
            invalidate();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            if (target == null) {
                canvas.drawColor(shade.getColor());
                return;
            }
            RectF cutout = new RectF(target);
            float radius = dp(12);
            canvas.save();
            Path cutoutPath = new Path();
            cutoutPath.addRoundRect(cutout, radius, radius, Path.Direction.CW);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                canvas.clipOutPath(cutoutPath);
            } else {
                canvas.clipPath(cutoutPath, Region.Op.DIFFERENCE);
            }
            canvas.drawColor(shade.getColor());
            canvas.restore();
            canvas.drawRoundRect(cutout, radius, radius, border);
            if (card != null) {
                float x = Math.max(card.left + dp(28), Math.min(card.right - dp(28), target.exactCenterX()));
                float y = cardBelow ? card.top : card.bottom;
                Path path = new Path();
                if (cardBelow) {
                    path.moveTo(x, y - dp(9));
                    path.lineTo(x - dp(9), y);
                    path.lineTo(x + dp(9), y);
                } else {
                    path.moveTo(x, y + dp(9));
                    path.lineTo(x - dp(9), y);
                    path.lineTo(x + dp(9), y);
                }
                path.close();
                canvas.drawPath(path, arrow);
            }
        }

        private int dp(int value) {
            return Math.round(value * getResources().getDisplayMetrics().density);
        }
    }
}
