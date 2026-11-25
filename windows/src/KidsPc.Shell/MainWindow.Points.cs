using System.Windows;
using KidsPc.Shared;

namespace KidsPc.Shell;

/// <summary>
/// Skylight chore points: his balance next to the time left, and a button that spends points on extra minutes
/// (also on the time's-up screen). The hub does the sums and says no in plain words (price, daily cap, bedtime);
/// the points come off in Skylight before the minutes go on. Hidden when a parent hasn't linked Skylight.
/// </summary>
public partial class MainWindow
{
    private bool _buying;
    private DateTimeOffset _statusHold = DateTimeOffset.MinValue;   // keep "Done! 15 more minutes" up for a few seconds

    private void ShowPoints(Rules rules)
    {
        var p = rules.Points;
        PointsCard.Visibility = p is null ? Visibility.Collapsed : Visibility.Visible;
        if (p is null) { LockBuy.Visibility = Visibility.Collapsed; return; }
        PointsBalance.Text = p.Balance.ToString("N0");
        var label = $"Buy {p.PriceMinutes} min for {p.PricePoints} points";
        BuyTimeButton.Content = label;
        BuyTimeButton.Visibility = p.CanBuy ? Visibility.Visible : Visibility.Collapsed;
        BuyTimeButton.IsEnabled = !_buying;
        PointsWhy.Text = p.Why;
        PointsWhy.Visibility = p.CanBuy ? Visibility.Collapsed : Visibility.Visible;
        LockBuyText.Text = $"Use {p.PricePoints} points for {p.PriceMinutes} more minutes";
        // On the lock screen only when time ran out (not bedtime or a pause) and he can afford it.
        LockBuy.Visibility = p.CanBuy && rules.Time.LockReason == "time" ? Visibility.Visible : Visibility.Collapsed;
        LockBuy.IsEnabled = !_buying;
    }

    private async void BuyTime(object s, RoutedEventArgs e)
    {
        if (_buying || _state?.Rules?.Points is null) return;
        _buying = true;
        BuyTimeButton.IsEnabled = LockBuy.IsEnabled = false;
        var target = LockOverlay.Visibility == Visibility.Visible ? LockStatus : StatusLine;
        try
        {
            var r = await _helper.GetAsync<BoughtTime>(new PipeRequest("buy_time"));
            target.Text = r is null ? "Done!" : $"Done! {r.Minutes} more minutes. You have {r.Balance:N0} points left.";
            StatusLine.Text = target.Text;
            _statusHold = DateTimeOffset.Now.AddSeconds(10);
        }
        catch (Exception ex) { target.Text = Plain(ex); }
        finally { _buying = false; }
        await RefreshAsync();
    }
}
