using KidsPc.Helper;
using KidsPc.Shared;

// Runs as a Windows service under LocalSystem (installed by setup\Install-KidPc.ps1).
// It is the only process holding admin rights and the hub's device token.
var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(o => o.ServiceName = "KidsPcHelper");
builder.Services.AddSingleton(DeviceConfig.Load());
builder.Services.AddSingleton(sp =>
{
    var cfg = sp.GetRequiredService<DeviceConfig>();
    var http = new HttpClient { BaseAddress = new Uri(cfg.HubUrl), Timeout = TimeSpan.FromSeconds(10) };
    http.DefaultRequestHeaders.Authorization = new("Bearer", cfg.DeviceToken);
    return http;
});
builder.Services.AddSingleton<StateCache>();
builder.Services.AddSingleton<GameLibrary>();
builder.Services.AddSingleton<Enforcer>();
builder.Services.AddSingleton<ChromePolicy>();
builder.Services.AddSingleton<AppInstaller>();
builder.Services.AddSingleton<EventQueue>();
builder.Services.AddSingleton<CommandRunner>();
builder.Services.AddSingleton<Updater>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<Updater>());
builder.Services.AddHostedService<HubSync>();
builder.Services.AddHostedService<PipeServer>();
builder.Build().Run();
