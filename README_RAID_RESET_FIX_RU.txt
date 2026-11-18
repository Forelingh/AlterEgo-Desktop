AlterEgo Desktop 1.0.2 — исправление Raid CD после недельного сброса.
Причина: AlterEgo.lua может хранить старые savedInstances с locked=true даже после истечения expires.
Исправление: учитываются только сохранения с expires > текущее Unix-время.
Сохранены цвета классов, иконка рыбы, название UoK, настройки и ручной Update.
Соберите BUILD_WINDOWS.bat и проверьте Raid CD. Не публикуйте без проверки на Windows.
