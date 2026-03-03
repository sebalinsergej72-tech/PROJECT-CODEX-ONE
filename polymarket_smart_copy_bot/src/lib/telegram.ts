import { useState, useEffect } from "react";

export function useTelegramTheme() {
    const [themeParams, setThemeParams] = useState<any>(null);

    useEffect(() => {
        if (window.Telegram?.WebApp) {
            setThemeParams(window.Telegram.WebApp.themeParams);

            const onThemeChanged = () => {
                setThemeParams(window.Telegram.WebApp.themeParams);
            };

            window.Telegram.WebApp.onEvent('themeChanged', onThemeChanged);
            return () => {
                window.Telegram.WebApp.offEvent('themeChanged', onThemeChanged);
            };
        }
    }, []);

    return themeParams;
}

// Ensure the Telegram window property is typed
declare global {
    interface Window {
        Telegram?: {
            WebApp?: any;
        };
    }
}
