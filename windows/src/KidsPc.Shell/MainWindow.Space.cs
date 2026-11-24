using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;

namespace KidsPc.Shell;

/// <summary>The retro-space backdrop (nebula glow, a busy starfield, planets, a shooting star) and the news ticker.</summary>
public partial class MainWindow
{
    private bool _spaceDrawn;

    private static Color Hex(string s) => (Color)ColorConverter.ConvertFromString(s);

    private void DrawSpace()
    {
        if (_spaceDrawn || ActualWidth < 10) return;
        _spaceDrawn = true;
        var rnd = new Random(7);   // same sky every time
        double w = ActualWidth, h = ActualHeight;

        // Nebula glow, far back.
        Stars.Children.Add(Glow(w * 0.80, h * 0.05, w * 0.55, "#5A1A9C", 0.50));
        Stars.Children.Add(Glow(w * 0.05, h * 0.95, w * 0.50, "#0B4A8F", 0.45));
        Stars.Children.Add(Glow(w * 0.95, h * 0.70, w * 0.40, "#8A155A", 0.40));

        // Stars: mostly white specks, some coloured, a few big four-point sparkles.
        string[] tints = ["#FFFFFF", "#FFFFFF", "#FFFFFF", "#C9F6FF", "#FFE9A8", "#FFB3DD"];
        for (var i = 0; i < 300; i++)
        {
            var size = rnd.NextDouble() < 0.10 ? 3.4 : rnd.NextDouble() * 1.8 + 0.8;
            var star = new Ellipse
            {
                Width = size, Height = size,
                Fill = new SolidColorBrush(Hex(tints[rnd.Next(tints.Length)])),
                Opacity = rnd.NextDouble() * 0.6 + 0.35,
            };
            Place(star, rnd.NextDouble() * w, rnd.NextDouble() * h);
            if (rnd.NextDouble() < 0.25) Twinkle(star, rnd);
            Stars.Children.Add(star);
        }
        string[] neon = ["#22E4FF", "#FFD23F", "#FF2E97", "#FFFFFF"];
        for (var i = 0; i < 16; i++)
        {
            var sparkle = Sparkle(rnd.NextDouble() * 14 + 10, Hex(neon[i % neon.Length]));
            sparkle.Opacity = 0.9;
            Place(sparkle, rnd.NextDouble() * w, rnd.NextDouble() * h);
            Twinkle(sparkle, rnd);
            Stars.Children.Add(sparkle);
        }

        // Planets, kept to the edges so they frame the tiles instead of hiding behind them.
        Stars.Children.Add(RingedPlanet(w * 0.90, h * 0.84, 360));
        Stars.Children.Add(Moon(w * 0.045, h * 0.90, 190));
        Stars.Children.Add(Ball(w * 0.60, h * 0.065, 46, "#D9FBFF", "#22E4FF", "#1B3BFF"));
        Stars.Children.Add(Ball(w * 0.385, h * 0.93, 30, "#E2FFD9", "#7DFF5A", "#0A7A4A"));

        Stars.Children.Add(ShootingStar(w, h));
    }

    private static void Place(UIElement e, double x, double y) { Canvas.SetLeft(e, x); Canvas.SetTop(e, y); }

    private static void Twinkle(UIElement e, Random rnd)
    {
        var twinkle = new DoubleAnimation(e.Opacity, 0.12, TimeSpan.FromSeconds(1 + rnd.NextDouble() * 2.5))
        {
            AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever, BeginTime = TimeSpan.FromSeconds(rnd.NextDouble() * 3),
        };
        Timeline.SetDesiredFrameRate(twinkle, 20);   // gentle on the GPU while a game runs underneath
        e.BeginAnimation(OpacityProperty, twinkle);
    }

    private static UIElement Glow(double cx, double cy, double d, string colour, double opacity)
    {
        var c = Hex(colour);
        var e = new Ellipse
        {
            Width = d, Height = d * 0.7, Opacity = opacity,
            Fill = new RadialGradientBrush(c, Color.FromArgb(0, c.R, c.G, c.B)),
        };
        Place(e, cx - d / 2, cy - d * 0.35);
        return e;
    }

    /// <summary>A four-point star, the kind that glints.</summary>
    private static Shape Sparkle(double size, Color c)
    {
        double m = size / 2, t = size * 0.09;
        return new Polygon
        {
            Width = size, Height = size, Fill = new SolidColorBrush(c),
            Points = new PointCollection
            {
                new Point(m, 0), new Point(m + t, m - t), new Point(size, m), new Point(m + t, m + t),
                new Point(m, size), new Point(m - t, m + t), new Point(0, m), new Point(m - t, m - t),
            },
        };
    }

    private static Ellipse Sphere(double d, string light, string mid, string dark) => new()
    {
        Width = d, Height = d,
        Fill = new RadialGradientBrush(new GradientStopCollection
        {
            new GradientStop(Hex(light), 0), new GradientStop(Hex(mid), 0.45), new GradientStop(Hex(dark), 1),
        }) { GradientOrigin = new Point(0.3, 0.28), Center = new Point(0.4, 0.4), RadiusX = 0.75, RadiusY = 0.75 },
    };

    private static UIElement Ball(double cx, double cy, double d, string light, string mid, string dark)
    {
        var s = Sphere(d, light, mid, dark);
        s.Effect = new System.Windows.Media.Effects.DropShadowEffect { Color = Hex(mid), BlurRadius = d * 0.6, ShadowDepth = 0, Opacity = 0.7 };
        Place(s, cx - d / 2, cy - d / 2);
        return s;
    }

    /// <summary>The big one: a striped pink giant with a cyan ring that passes behind it and back in front.</summary>
    private static UIElement RingedPlanet(double cx, double cy, double d)
    {
        double rw = d * 1.9, rh = d * 0.46, box = rw;
        var g = new Canvas { Width = box, Height = box, RenderTransform = new RotateTransform(-18, box / 2, box / 2) };

        Ellipse Ring(string colour) => new()
        {
            Width = rw, Height = rh, StrokeThickness = d * 0.055, Stroke = new SolidColorBrush(Hex(colour)),
        };
        var back = Ring("#0FA8C4");
        Place(back, 0, (box - rh) / 2);
        g.Children.Add(back);

        var body = Sphere(d, "#FFD1EC", "#FF2E97", "#4A0A8F");
        body.Effect = new System.Windows.Media.Effects.DropShadowEffect { Color = Hex("#FF2E97"), BlurRadius = d * 0.35, ShadowDepth = 0, Opacity = 0.75 };
        Place(body, (box - d) / 2, (box - d) / 2);
        g.Children.Add(body);

        // Cloud bands, clipped to the disc.
        var bands = new Canvas { Width = d, Height = d, Clip = new EllipseGeometry(new Point(d / 2, d / 2), d / 2, d / 2), Opacity = 0.28 };
        (double Y, double H, string C)[] stripes = [(0.16, 0.07, "#FFFFFF"), (0.33, 0.05, "#4A0A8F"), (0.47, 0.10, "#FFFFFF"), (0.66, 0.06, "#4A0A8F"), (0.80, 0.05, "#FFFFFF")];
        foreach (var (y, sh, c) in stripes)
        {
            var r = new Rectangle { Width = d, Height = d * sh, Fill = new SolidColorBrush(Hex(c)) };
            Place(r, 0, d * y);
            bands.Children.Add(r);
        }
        Place(bands, (box - d) / 2, (box - d) / 2);
        g.Children.Add(bands);

        var front = Ring("#22E4FF");
        front.Clip = new RectangleGeometry(new Rect(0, rh / 2, rw, rh));   // lower half only: the part in front of the planet
        front.Effect = new System.Windows.Media.Effects.DropShadowEffect { Color = Hex("#22E4FF"), BlurRadius = 16, ShadowDepth = 0, Opacity = 0.8 };
        Place(front, 0, (box - rh) / 2);
        g.Children.Add(front);

        Place(g, cx - box / 2, cy - box / 2);
        return g;
    }

    private static UIElement Moon(double cx, double cy, double d)
    {
        var g = new Canvas { Width = d, Height = d };
        var body = Sphere(d, "#FFF7C2", "#FFD23F", "#C24A00");
        body.Effect = new System.Windows.Media.Effects.DropShadowEffect { Color = Hex("#FFD23F"), BlurRadius = d * 0.3, ShadowDepth = 0, Opacity = 0.6 };
        g.Children.Add(body);
        (double X, double Y, double S)[] craters = [(0.22, 0.20, 0.22), (0.58, 0.44, 0.14), (0.32, 0.60, 0.18), (0.66, 0.20, 0.09), (0.60, 0.72, 0.10)];
        foreach (var (x, y, s) in craters)
        {
            var c = new Ellipse { Width = d * s, Height = d * s, Fill = new SolidColorBrush(Hex("#40A34A00")) };
            Place(c, d * x, d * y);
            g.Children.Add(c);
        }
        Place(g, cx - d / 2, cy - d / 2);
        return g;
    }

    /// <summary>A streak that crosses the sky for a second, then waits a while before the next pass.</summary>
    private static UIElement ShootingStar(double w, double h)
    {
        var move = new TranslateTransform(w * 0.25, -40);
        var path = new TransformGroup();
        path.Children.Add(new RotateTransform(24));
        path.Children.Add(move);
        var streak = new Rectangle
        {
            Width = 220, Height = 3, RadiusX = 1.5, RadiusY = 1.5, Opacity = 0,
            Fill = new LinearGradientBrush(Color.FromArgb(0, 255, 255, 255), Colors.White, 0),
            RenderTransform = path,
        };
        var period = TimeSpan.FromSeconds(11);
        DoubleAnimationUsingKeyFrames Keys(params (double Value, double At)[] frames)
        {
            var a = new DoubleAnimationUsingKeyFrames { Duration = period, RepeatBehavior = RepeatBehavior.Forever, BeginTime = TimeSpan.FromSeconds(4) };
            foreach (var (value, at) in frames) a.KeyFrames.Add(new LinearDoubleKeyFrame(value, KeyTime.FromTimeSpan(TimeSpan.FromSeconds(at))));
            Timeline.SetDesiredFrameRate(a, 30);
            return a;
        }
        move.BeginAnimation(TranslateTransform.XProperty, Keys((w * 0.25, 0), (w * 0.25 + 900, 1.1), (w * 0.25 + 900, 11)));
        move.BeginAnimation(TranslateTransform.YProperty, Keys((-40, 0), (360, 1.1), (360, 11)));
        streak.BeginAnimation(OpacityProperty, Keys((0, 0), (0.9, 0.15), (0.9, 0.8), (0, 1.1), (0, 11)));
        return streak;
    }

    // ------------------------------------------------------------------ ticker

    private string? _tickerFor;
    private AnimationClock? _tickerClock;

    /// <summary>A line of friendly reminders along the bottom edge. It sits still for a moment, then drifts
    /// right to left like an old web page marquee. It rests while a game is in front.</summary>
    private void SetTicker(string kidName)
    {
        if (_tickerFor == kidName) return;
        if (_tickerFor is null)
        {
            Activated += (_, _) => { if (_tickerClock is { IsPaused: true } c) c.Controller?.Resume(); };
            Deactivated += (_, _) => _tickerClock?.Controller?.Pause();
        }
        _tickerFor = kidName;
        const string dot = "   \u2022   ";
        Ticker.Text = string.Join(dot,
            $"Welcome to {kidName}'s galaxy",
            "Save your game before the time runs out",
            "Want a new game? Press Get more games and ask Mom or Dad",
            "Need to tell Mom or Dad something? Family chat is in the corner",
            "Be a good teammate") + dot;
        Ticker.UpdateLayout();
        ScrollTicker(48, 20);
    }

    private void ScrollTicker(double from, double delaySeconds)
    {
        var width = Math.Max(Ticker.ActualWidth, 200);
        var scroll = new DoubleAnimation(from, -width, TimeSpan.FromSeconds((from + width) / 70))
        {
            BeginTime = TimeSpan.FromSeconds(delaySeconds),
        };
        Timeline.SetDesiredFrameRate(scroll, 30);
        scroll.Completed += (_, _) => ScrollTicker(Math.Max(ActualWidth, 800), 0);
        _tickerClock = scroll.CreateClock();
        Ticker.ApplyAnimationClock(Canvas.LeftProperty, _tickerClock);
        if (!IsActive) _tickerClock.Controller?.Pause();
    }
}
