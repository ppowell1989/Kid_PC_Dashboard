using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;

namespace KidsPc.Shell;

/// <summary>The picture behind the cards. Each theme names one in its "Backdrop" entry: soft washes of colour,
/// a starfield with planets, a blocky landscape, beams of light, or a race track. All original drawings made
/// from plain shapes. Also the bottom ticker, for themes that show it.</summary>
public partial class MainWindow
{
    private static Color Hex(string s) => (Color)ColorConverter.ConvertFromString(s);

    private void DrawBackdrop()
    {
        if (ActualWidth < 10) return;
        double w = ActualWidth, h = ActualHeight;
        Backdrop.Children.Clear();
        switch (TryFindResource("Backdrop") as string)
        {
            case "space": DrawSpace(w, h); break;
            case "blocks": DrawBlocks(w, h); break;
            case "rays": DrawRays(w, h); break;
            case "track": DrawTrack(w, h); break;
            default:
                Backdrop.Children.Add(Glow(w * 0.92, h * 0.02, w * 0.70, "#BFD2FF", 0.55));   // blue, top right
                Backdrop.Children.Add(Glow(w * 0.02, h * 1.00, w * 0.60, "#C4F1E0", 0.50));   // mint, bottom left
                Backdrop.Children.Add(Glow(w * 0.98, h * 1.02, w * 0.50, "#FFE1C7", 0.50));   // peach, bottom right
                break;
        }
    }

    private void Box(double x, double y, double bw, double bh, string colour, double opacity = 1)
    {
        var r = new Rectangle { Width = bw, Height = bh, Fill = new SolidColorBrush(Hex(colour)), Opacity = opacity, SnapsToDevicePixels = true };
        Place(r, x, y);
        Backdrop.Children.Add(r);
    }

    /// <summary>A blocky landscape: square sun, square clouds, and a strip of grass over dirt along the bottom,
    /// built from 48-pixel cubes with a few lighter and darker ones mixed in.</summary>
    private void DrawBlocks(double w, double h)
    {
        var rnd = new Random(11);
        const double s = 48;
        Box(w * 0.80, h * 0.07, 120, 120, "#FFF3B0");
        Box(w * 0.80 + 12, h * 0.07 + 12, 96, 96, "#FFE066");
        (double X, double Y, int N)[] clouds = [(0.36, 0.05, 5), (0.58, 0.16, 4), (0.12, 0.30, 3), (0.90, 0.36, 4)];
        foreach (var (cx, cy, n) in clouds)
        {
            Box(w * cx, h * cy, n * s, s, "#FFFFFF", 0.85);
            Box(w * cx + s, h * cy - s * 0.5, (n - 2) * s, s * 0.5, "#FFFFFF", 0.85);
        }
        var top = h - 3 * s;
        string[] grass = ["#5DAE3B", "#56A335", "#66B844"], dirt = ["#8A5A3A", "#7D5033", "#946343"];
        for (double x = 0; x < w; x += s)
        {
            var bump = rnd.NextDouble() < 0.18 ? s : 0;   // the odd block standing proud of the ground
            if (bump > 0) Box(x, top - s, s, s, grass[rnd.Next(3)]);
            Box(x, top, s, s, bump > 0 ? dirt[rnd.Next(3)] : grass[rnd.Next(3)]);
            Box(x, top, s, 5, "#3F8427", bump > 0 ? 0 : 0.5);
            Box(x, top + s, s, s, dirt[rnd.Next(3)]);
            Box(x, top + 2 * s, s, s, rnd.NextDouble() < 0.2 ? "#6E6E6E" : dirt[rnd.Next(3)]);
        }
    }

    /// <summary>Wide beams of light fanning out from the bottom left corner.</summary>
    private void DrawRays(double w, double h)
    {
        Backdrop.Children.Add(Glow(w * 0.85, h * 0.10, w * 0.70, "#FF6FD8", 0.35));
        Backdrop.Children.Add(Glow(w * 0.10, h * 0.95, w * 0.60, "#40C4FF", 0.40));
        for (var i = 0; i < 7; i++)
        {
            var beam = new Polygon
            {
                Points = new PointCollection { new Point(0, h), new Point(w * 1.6, h - 60), new Point(w * 1.6, h + 60) },
                Fill = new SolidColorBrush(Colors.White), Opacity = i % 2 == 0 ? 0.05 : 0.03,
                RenderTransform = new RotateTransform(-8 - i * 12, 0, h),
            };
            Backdrop.Children.Add(beam);
        }
    }

    /// <summary>Racing stripes sweeping across the top corner and a chequered strip along the bottom edge.</summary>
    private void DrawTrack(double w, double h)
    {
        Backdrop.Children.Add(Glow(w * 0.90, h * 0.05, w * 0.60, "#FF6A00", 0.16));
        (double Off, double Wd, string C, double O)[] stripes = [(0, 46, "#FF6A00", 0.85), (70, 16, "#FFFFFF", 0.5), (104, 30, "#1E90FF", 0.8)];
        foreach (var (off, wd, c, o) in stripes)
        {
            var stripe = new Rectangle
            {
                Width = w, Height = wd, Fill = new SolidColorBrush(Hex(c)), Opacity = o * 0.5,
                RenderTransform = new RotateTransform(-24, 0, 0),
            };
            Place(stripe, w * 0.56, h * 0.42 + off);
            Backdrop.Children.Add(stripe);
        }
        const double s = 18;
        for (var row = 0; row < 2; row++)
            for (double x = 0; x < w; x += s)
                Box(x, h - (2 - row) * s, s, s, ((int)(x / s) + row) % 2 == 0 ? "#F2F4F7" : "#14171C", 0.5);
    }

    private void DrawSpace(double w, double h)
    {
        var rnd = new Random(7);   // same sky every time

        // Nebula glow, far back.
        Backdrop.Children.Add(Glow(w * 0.80, h * 0.05, w * 0.55, "#5A1A9C", 0.50));
        Backdrop.Children.Add(Glow(w * 0.05, h * 0.95, w * 0.50, "#0B4A8F", 0.45));
        Backdrop.Children.Add(Glow(w * 0.95, h * 0.70, w * 0.40, "#8A155A", 0.40));

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
            Backdrop.Children.Add(star);
        }
        string[] neon = ["#22E4FF", "#FFD23F", "#FF2E97", "#FFFFFF"];
        for (var i = 0; i < 16; i++)
        {
            var sparkle = Sparkle(rnd.NextDouble() * 14 + 10, Hex(neon[i % neon.Length]));
            sparkle.Opacity = 0.9;
            Place(sparkle, rnd.NextDouble() * w, rnd.NextDouble() * h);
            Twinkle(sparkle, rnd);
            Backdrop.Children.Add(sparkle);
        }

        // Planets, kept to the edges so they frame the tiles instead of hiding behind them.
        Backdrop.Children.Add(RingedPlanet(w * 0.90, h * 0.84, 360));
        Backdrop.Children.Add(Moon(w * 0.045, h * 0.90, 190));
        Backdrop.Children.Add(Ball(w * 0.60, h * 0.065, 46, "#D9FBFF", "#22E4FF", "#1B3BFF"));
        Backdrop.Children.Add(Ball(w * 0.385, h * 0.93, 30, "#E2FFD9", "#7DFF5A", "#0A7A4A"));

        Backdrop.Children.Add(ShootingStar(w, h));
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
    private bool _tickerHooked;
    private AnimationClock? _tickerClock;

    /// <summary>A line of friendly reminders along the bottom edge, in themes that show it. It sits still for a
    /// moment, then drifts right to left like an old web page marquee. It rests while a game is in front.</summary>
    private void SetTicker(string kidName)
    {
        if (TryFindResource("TickerVisibility") is not Visibility.Visible)
        {
            if (_tickerClock is not null) { Ticker.ApplyAnimationClock(Canvas.LeftProperty, null); _tickerClock = null; }
            _tickerFor = null;
            return;
        }
        if (_tickerFor == kidName) return;
        if (!_tickerHooked)
        {
            _tickerHooked = true;
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
        scroll.Completed += (_, _) => { if (_tickerFor is not null) ScrollTicker(Math.Max(ActualWidth, 800), 0); };
        _tickerClock = scroll.CreateClock();
        Ticker.ApplyAnimationClock(Canvas.LeftProperty, _tickerClock);
        if (!IsActive) _tickerClock.Controller?.Pause();
    }
}
