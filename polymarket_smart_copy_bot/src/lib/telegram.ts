import { useState, useEffect } from "react";

import { authenticateTelegramWebApp } from "./api";

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

export async function bootstrapTelegramWebAppAuth() {
    const webApp = window.Telegram?.WebApp;
    if (!webApp?.initData) return;

    try {
        localStorage.removeItem("bot_api_url");
        const auth = await authenticateTelegramWebApp(webApp.initData);
        if (auth.dashboard_token) {
            localStorage.setItem("dashboard_session_token", auth.dashboard_token);
        }
    } catch (error) {
        console.warn("Telegram WebApp auth failed", error);
        localStorage.removeItem("dashboard_session_token");
    }
}

// Ensure the Telegram window property is typed
declare global {
    interface Window {
        Telegram?: {
            WebApp?: {
                initData?: string;
                initDataUnsafe?: unknown;
                themeParams?: unknown;
                ready: () => void;
                expand: () => void;
                onEvent: (event: string, cb: () => void) => void;
                offEvent: (event: string, cb: () => void) => void;
            };
        };
    }
}
