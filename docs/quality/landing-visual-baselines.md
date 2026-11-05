# Landing 新屏幕基线记录

用户Q14批准完整视觉、Q15批准真实接入。此处只引入新屏幕，不刷新既有工作台截图。

- Windows本地Chrome：landing-mobile-win32.png、stamp-desktop-win32.png；真实生产Next页面，手机375×812、桌面1280×720，reduced-motion静态场景，等待本地字体与图片。
- Linux：fork GitHub Actions run41973717483，head a9f81cba372409a95781ce32cab0d89f801a360f，root artifact13390178651。原始artifactZIP SHA2565ce0e3d087107e76db2f070bab83f16cc2206619006170a8e1dade5507795536；从上传的快照目录仅提取landing-mobile-linux.png、stamp-desktop-linux.png，无变形/修图。
- 走查：手机标题、校徽、入口、中轴取景与Windows/批准稿一致；桌面静态五条内容和原生305px新印面、暖纸水纹均完整。截图允许Linux字体光栅环境差异，不改变容忍度。
- 该次Linux其余9项功能/axe/资源/正常DONE测试通过；两项失败是预期的新基线缺失。补入后须再次由Linux运行比较，不把写新图当作比较已通过。
- 上游PR156的Actions需维护者批准，fork证据不能冒充上游运行。
