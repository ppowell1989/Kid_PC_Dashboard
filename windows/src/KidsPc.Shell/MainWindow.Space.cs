using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;

namespace KidsPc.Shell;

/// <summary>The retro-space backdrop: twinkling stars, a couple of neon planets with a ring.</summary>
public partial class MainWindow
{
    private bool _spaceDrawn;

    private void DrawSpace()
    {
        if (_spaceDrawn || ActualWidth < 10) return;
        _spaceDrawn = true;
        var rnd = new Random(7);   // same sky every time
        double w = ActualWidth, h = ActualHeight;

        // Planets first (behind the stars).
        Stars.Children.Add(Planet(w * 0.86, h * 0.80, 220, "#FF2E97", "#6A0DAD", ring: true));
        Stars.Children.Add(Planet(w * 0.70, h * 0.10, 70, "#22E4FF", "#1B3BFF", ring: false));
        Stars.Children.Add(Planet(w * 0.06, h * 0.92, 120, "#FFD23F", "#FF6A00", ring: false));

        for (var i = 0; i < 220; i++)
        {
            var size = rnd.NextDouble() < 0.08 ? 3.2 : rnd.NextDouble() * 1.8 + 0.6;
            var star = new Ellipse
            {
                Width = size, Height = size,
                Fill = new SolidColorBrush(rnd.NextDouble() < 0.15 ? Color.FromRgb(0xC9, 0xF6, 0xFF) : Colors.White),
                Opacity = rnd.NextDouble() * 0.6 + 0.3,
            };
            Canvas.SetLeft(star, rnd.NextDouble() * w);
            Canvas.SetTop(star, rnd.NextDouble() * h);
            if (rnd.NextDouble() < 0.25)
            {
                var twinkle = new DoubleAnimation(star.Opacity, 0.1, TimeSpan.FromSeconds(1 + rnd.NextDouble() * 2.5))
                {
                    AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever, BeginTime = TimeSpan.FromSeconds(rnd.NextDouble() * 3),
                };
                Timeline.SetDesiredFrameRate(twinkle, 20);   // gentle on the GPU while a game runs underneath
                star.BeginAnimation(OpacityProperty, twinkle);
            }
            Stars.Children.Add(star);
        }
    }

    private static UIElement Planet(double cx, double cy, double d, string light, string dark, bool ring)
    {
        var g = new Grid { Width = d * 2, Height = d * 2, Opacity = 0.55 };
        var body = new Ellipse
        {
            Width = d, Height = d,
            Fill = new RadialGradientBrush((Color)ColorConverter.ConvertFromString(light), (Color)ColorConverter.ConvertFromString(dark))
                { GradientOrigin = new Point(0.3, 0.3), Center = new Point(0.35, 0.35), RadiusX = 0.8, RadiusY = 0.8 },
            Effect = new System.Windows.Media.Effects.DropShadowEffect
                { Color = (Color)ColorConverter.ConvertFromString(light), BlurRadius = d / 3, ShadowDepth = 0, Opacity = 0.6 },
        };
        g.Children.Add(body);
        if (ring)
            g.Children.Add(new Ellipse
            {
                Width = d * 1.8, Height = d * 0.45, StrokeThickness = d * 0.05,
                Stroke = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#AA22E4FF")),
                RenderTransform = new RotateTransform(-18), RenderTransformOrigin = new Point(0.5, 0.5),
            });
        Canvas.SetLeft(g, cx - d);
        Canvas.SetTop(g, cy - d);
        return g;
    }
}
