"""Render state-machine effects in the explicitly selected conversation language."""

from __future__ import annotations

from app.orchestrator.state_machine import Effect, Say

_PHRASES: dict[str, dict[str, str]] = {
    "en-IN": {
        "greeting": "Hello. What groceries would you like?",
        "filler_search": "One moment, I am checking that.",
        "pack_confirm": "I used the requested quantity across these packs.",
        "confirm_reask": "Would you like me to place this order? Please say yes or no.",
        "ask_what_to_add": "What would you like to add?",
        "order_placed": "Your mock order has been placed.",
        "order_cancelled": "The current cart has been cancelled.",
        "order_already_placed": "That order has already been placed.",
        "unknown_user": "I could not identify this user.",
        "checking_order": "I am checking whether the order was placed.",
        "order_failed_retry": "The order was not confirmed. Please review the cart again.",
        "cart_edit_unclear": "I did not understand that change. Please say it again.",
        "min_order": "The cart is below the minimum order amount.",
        "cart_expired": "The cart expired. I will refresh it.",
        "cart_changed": "The cart changed. Please review it before confirming.",
        "swiggy_login_expired": "The commerce session expired.",
        "intent_error": "I could not understand that. Please say it again.",
        "clarification_pack_size": "Which pack size would you like?",
        "clarification_only_bigger_pack": "Only a larger pack is available. Would you like it?",
        "clarification_out_of_stock": "That item is not available. Would you like something else?",
        "clarification_ambiguous_item": "I found more than one match. Which item do you mean?",
        "clarification_min_order": (
            "The cart is below the minimum order amount. What would you like to add?"
        ),
        "readback": (
            "Your basket has {lines}. The total is {total} rupees. Payment is cash on delivery. "
            "Delivery address: {address}. Shall I confirm?"
        ),
    },
    "hi-IN": {
        "greeting": "नमस्ते। आपको कौन-सा सामान चाहिए?",
        "filler_search": "एक पल, मैं देख रही हूँ।",
        "pack_confirm": "आपकी बताई मात्रा इन पैकों में जोड़ दी है।",
        "confirm_reask": "क्या मैं यह ऑर्डर कर दूँ? कृपया हाँ या नहीं कहें।",
        "ask_what_to_add": "आप क्या जोड़ना चाहेंगे?",
        "order_placed": "आपका डेमो ऑर्डर पूरा हो गया है।",
        "order_cancelled": "आपकी अभी की टोकरी रद्द कर दी गई है।",
        "order_already_placed": "यह ऑर्डर पहले ही पूरा हो चुका है।",
        "unknown_user": "मैं आपकी पहचान नहीं कर पाई।",
        "checking_order": "मैं देख रही हूँ कि ऑर्डर पूरा हुआ या नहीं।",
        "order_failed_retry": "ऑर्डर पूरा नहीं हुआ। कृपया टोकरी फिर से देख लें।",
        "cart_edit_unclear": "मुझे बदलाव समझ नहीं आया। कृपया फिर से कहें।",
        "min_order": "टोकरी की रकम ऑर्डर की न्यूनतम रकम से कम है।",
        "cart_expired": "टोकरी की अवधि खत्म हो गई। मैं इसे फिर से देखती हूँ।",
        "cart_changed": "टोकरी में बदलाव हुआ है। पुष्टि से पहले इसे फिर से देख लें।",
        "swiggy_login_expired": "दुकान से संपर्क टूट गया है।",
        "intent_error": "मुझे समझ नहीं आया। कृपया फिर से कहें।",
        "clarification_pack_size": "आपको कौन-सा पैक चाहिए?",
        "clarification_only_bigger_pack": "सिर्फ बड़ा पैक उपलब्ध है। क्या आप उसे लेना चाहेंगे?",
        "clarification_out_of_stock": "यह सामान अभी उपलब्ध नहीं है। क्या आप कुछ और लेना चाहेंगे?",
        "clarification_ambiguous_item": "इस नाम के कई सामान मिले। आपको कौन-सा चाहिए?",
        "clarification_min_order": "टोकरी की रकम कम है। आप और क्या जोड़ना चाहेंगे?",
        "readback": (
            "आपकी टोकरी में {lines} हैं। कुल {total} रुपये हैं। भुगतान सामान मिलने पर नकद है। "
            "पता: {address}। क्या मैं ऑर्डर की पुष्टि करूँ?"
        ),
    },
    "kn-IN": {
        "greeting": "ನಮಸ್ಕಾರ. ನಿಮಗೆ ಯಾವ ಸಾಮಾನುಗಳು ಬೇಕು?",
        "filler_search": "ಒಂದು ಕ್ಷಣ, ನಾನು ಪರಿಶೀಲಿಸುತ್ತೇನೆ.",
        "pack_confirm": "ನೀವು ಕೇಳಿದ ಪ್ರಮಾಣಕ್ಕೆ ಈ ಪ್ಯಾಕ್‌ಗಳನ್ನು ಸೇರಿಸಿದ್ದೇನೆ.",
        "confirm_reask": "ಈ ಆರ್ಡರ್ ಮಾಡಬೇಕೇ? ದಯವಿಟ್ಟು ಹೌದು ಅಥವಾ ಬೇಡ ಎಂದು ಹೇಳಿ.",
        "ask_what_to_add": "ನೀವು ಇನ್ನೇನು ಸೇರಿಸಬೇಕು?",
        "order_placed": "ನಿಮ್ಮ ಡೆಮೊ ಆರ್ಡರ್ ಪೂರ್ಣವಾಗಿದೆ.",
        "order_cancelled": "ಈಗಿನ ಬುಟ್ಟಿಯನ್ನು ರದ್ದು ಮಾಡಲಾಗಿದೆ.",
        "order_already_placed": "ಈ ಆರ್ಡರ್ ಈಗಾಗಲೇ ಪೂರ್ಣವಾಗಿದೆ.",
        "unknown_user": "ನಿಮ್ಮನ್ನು ಗುರುತಿಸಲು ಆಗಲಿಲ್ಲ.",
        "checking_order": "ಆರ್ಡರ್ ಆಗಿದೆಯೇ ಎಂದು ಪರಿಶೀಲಿಸುತ್ತಿದ್ದೇನೆ.",
        "order_failed_retry": "ಆರ್ಡರ್ ಪೂರ್ಣವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಬುಟ್ಟಿಯನ್ನು ಮತ್ತೊಮ್ಮೆ ನೋಡಿ.",
        "cart_edit_unclear": "ನೀವು ಹೇಳಿದ ಬದಲಾವಣೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "min_order": "ಬುಟ್ಟಿಯ ಮೊತ್ತ ಕನಿಷ್ಠ ಆರ್ಡರ್ ಮೊತ್ತಕ್ಕಿಂತ ಕಡಿಮೆ ಇದೆ.",
        "cart_expired": "ಬುಟ್ಟಿಯ ಅವಧಿ ಮುಗಿದಿದೆ. ಮತ್ತೆ ಪರಿಶೀಲಿಸುತ್ತೇನೆ.",
        "cart_changed": "ಬುಟ್ಟಿಯಲ್ಲಿ ಬದಲಾವಣೆ ಆಗಿದೆ. ದೃಢೀಕರಿಸುವ ಮೊದಲು ಮತ್ತೊಮ್ಮೆ ನೋಡಿ.",
        "swiggy_login_expired": "ಅಂಗಡಿಯ ಸಂಪರ್ಕದ ಅವಧಿ ಮುಗಿದಿದೆ.",
        "intent_error": "ನನಗೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "clarification_pack_size": "ನಿಮಗೆ ಯಾವ ಗಾತ್ರದ ಪ್ಯಾಕ್ ಬೇಕು?",
        "clarification_only_bigger_pack": "ದೊಡ್ಡ ಪ್ಯಾಕ್ ಮಾತ್ರ ಲಭ್ಯವಿದೆ. ಅದನ್ನು ಸೇರಿಸಬೇಕೇ?",
        "clarification_out_of_stock": "ಆ ಸಾಮಾನು ಈಗ ಲಭ್ಯವಿಲ್ಲ. ಬೇರೆ ಏನಾದರೂ ಬೇಕೇ?",
        "clarification_ambiguous_item": "ಈ ಹೆಸರಿನ ಒಂದಕ್ಕಿಂತ ಹೆಚ್ಚು ಸಾಮಾನು ಸಿಕ್ಕಿವೆ. ನಿಮಗೆ ಯಾವುದು ಬೇಕು?",
        "clarification_min_order": "ಬುಟ್ಟಿಯ ಮೊತ್ತ ಕಡಿಮೆ ಇದೆ. ಇನ್ನೇನು ಸೇರಿಸಬೇಕು?",
        "readback": (
            "ನಿಮ್ಮ ಬುಟ್ಟಿಯಲ್ಲಿ {lines} ಇವೆ. ಒಟ್ಟು {total} ರೂಪಾಯಿ. ಪಾವತಿ ಸಾಮಾನು ಬಂದಾಗ ನಗದು. "
            "ವಿಳಾಸ: {address}. ಆರ್ಡರ್ ಖಚಿತಪಡಿಸಲೇ?"
        ),
    },
}


def response_text(effects: list[Effect], *, language: str) -> str:
    """Render only application-owned language templates, never model error prose."""
    phrases = _PHRASES.get(language)
    if phrases is None:
        raise ValueError("Language must be kn-IN, hi-IN, or en-IN")

    parts: list[str] = []
    for effect in effects:
        if not isinstance(effect, Say):
            continue
        if effect.template == "readback":
            parts.append(
                phrases["readback"].format(
                    lines=effect.data.get("lines", ""),
                    total=effect.data.get("total", ""),
                    address=effect.data.get("address", effect.data.get("address_label", "")),
                )
            )
        elif effect.template == "clarification":
            kind = effect.data.get("kind", "ambiguous_item")
            parts.append(phrases.get(f"clarification_{kind}", phrases["intent_error"]))
        else:
            parts.append(phrases.get(effect.template, phrases["intent_error"]))
    return " ".join(part for part in parts if part).strip()


def speech_error_text(language: str) -> str:
    """Localized notice shown when synthesis fails; never substitute another voice."""
    return {
        "kn-IN": "ಧ್ವನಿ ಉತ್ತರ ಸಿದ್ಧವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮೇಲಿನ ಉತ್ತರವನ್ನು ಓದಿ.",
        "hi-IN": "आवाज़ में जवाब तैयार नहीं हुआ। कृपया ऊपर लिखा जवाब पढ़ें।",
        "en-IN": "Ajji could not make an audio reply. Please read the message above.",
    }[language]
