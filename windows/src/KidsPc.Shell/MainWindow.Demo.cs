using System.Windows;

namespace KidsPc.Shell;

/// <summary>Walks the window to the screen a demo scene asks for (see Demo.cs).</summary>
public partial class MainWindow
{
    private async Task ShowDemoSceneAsync(string scene)
    {
        switch (scene)
        {
            case "catalogue":
            case "ask":
                ShowCatalogue(this, new RoutedEventArgs());
                AskGameBox.Text = "space";
                await SearchGamesAsync();
                if (scene == "ask")
                {
                    var hits = await _helper.GetAsync<List<KidsPc.Shared.SteamGame>>(new KidsPc.Shared.PipeRequest("game_search", Detail: "space"));
                    ShowAsk(hits![2]);
                    AskNote.Text = "Barrett has it and we want to build rockets together";
                }
                break;
            case "chat":
                ToggleChat(this, new RoutedEventArgs());
                break;
            case "power":
                OpenPower(this, new RoutedEventArgs());
                break;
            case "themes":
                OpenThemes(this, new RoutedEventArgs());
                break;
            case "voice":
                OpenVoice();   // voice.html shows a made-up room when the token says "demo"
                break;
        }
    }
}
