using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shapes;

namespace KidsPc.Shell;

/// <summary>The backdrop: three large, very soft washes of colour behind the cards. Nothing moves.</summary>
public partial class MainWindow
{
    private void DrawBackdrop()
    {
        if (ActualWidth < 10) return;
        double w = ActualWidth, h = ActualHeight;
        Backdrop.Children.Clear();
        Backdrop.Children.Add(Wash(w * 0.92, h * 0.02, w * 0.70, "#BFD2FF", 0.55));   // blue, top right
        Backdrop.Children.Add(Wash(w * 0.02, h * 1.00, w * 0.60, "#C4F1E0", 0.50));   // mint, bottom left
        Backdrop.Children.Add(Wash(w * 0.98, h * 1.02, w * 0.50, "#FFE1C7", 0.50));   // peach, bottom right
    }

    private static UIElement Wash(double cx, double cy, double d, string colour, double opacity)
    {
        var c = (Color)ColorConverter.ConvertFromString(colour);
        var e = new Ellipse
        {
            Width = d, Height = d * 0.8, Opacity = opacity,
            Fill = new RadialGradientBrush(c, Color.FromArgb(0, c.R, c.G, c.B)),
        };
        Canvas.SetLeft(e, cx - d / 2);
        Canvas.SetTop(e, cy - d * 0.4);
        return e;
    }
}
