"""Render state-machine effects in the explicitly selected conversation language."""

from __future__ import annotations

import re

from app.orchestrator.state_machine import Effect, Say, format_money

_PHRASES: dict[str, dict[str, str]] = {
    "en-IN": {
        "greeting": "Hello. What groceries would you like?",
        "filler_search": "One moment, I am checking that.",
        "pack_confirm": "I used the requested quantity across these packs.",
        "confirm_reask": "Would you like me to place this order? Please say yes or no.",
        "ask_what_to_add": "What would you like to add?",
        "what_to_change": "All right, nothing was ordered. What would you like to change?",
        "existing_cart": "Your Swiggy basket already has some items. I will keep them.",
        "item_skipped": "I left that item out.",
        "order_placed": "Your demo order is complete. This was not a real order.",
        "order_placed_real": "Your Instamart order is placed and I have checked it with Swiggy.",
        "order_placed_unverified": "Your demo order was placed, but I could not check it.",
        "order_placed_unverified_real": (
            "Swiggy reported your order as placed, but I could not check it yet. "
            "Please look in the Swiggy app. Do not order again."
        ),
        "order_partial": "Only part of your demo order went through.",
        "order_partial_real": (
            "Only part of your order was placed. Please check the Swiggy app. "
            "I will not try again by myself."
        ),
        "order_problem": "There is a problem with your demo order.",
        "order_problem_real": (
            "Swiggy shows a problem with this order. Please check the Swiggy app or call "
            "customer care."
        ),
        "order_unknown": (
            "I could not confirm whether your order was placed. Please do not order again. "
            "Check the Swiggy app, or ask someone to help you."
        ),
        "order_rejected": "Swiggy did not accept the order, and no order was placed.",
        "order_not_attempted": "I did not place the order, because I could not check the basket.",
        "order_limit": "I did not place the order, because it is above the allowed limit.",
        "checkout_disabled": "Ordering is turned off right now, so no order was placed.",
        "order_cancelled": "The current basket has been cancelled.",
        "order_already_placed": "That order has already been placed.",
        "order_in_progress": "Please wait, I am still working on your order.",
        "cancel_via_support": "To cancel your order, please call Swiggy customer care at 080-67466729.",
        "unknown_user": "I could not identify this user.",
        "checking_order": "I am checking whether the order was placed.",
        "cart_edit_unclear": "I did not understand that change. Please say it again.",
        "cart_changed": "The basket changed. Please review it before confirming.",
        "cart_writes_disabled": "Changing the basket is turned off right now.",
        "commerce_error": "I could not reach the shop just now. Please try again.",
        "not_serviceable": "The shop does not deliver to this address right now.",
        "item_unavailable": "One of those items is not available right now.",
        "swiggy_login_expired": "The Swiggy login has expired, so someone needs to sign in again.",
        "intent_error": "I could not understand that. Please say it again.",
        "clarification_pack_size": "Which pack size of {item} would you like?",
        "clarification_only_bigger_pack": "Only a larger pack of {item} is available. Would you like it?",
        "clarification_out_of_stock": "{item} is not available. Would you like something else?",
        "clarification_ambiguous_item": "I found more than one {item}. Which do you mean?",
        "clarification_not_found": "I could not find {item}. What should I add instead?",
        "clarification_which_line": "Which item in your basket do you mean?",
        "clarification_usual_items": "Which of your usual items should I add?",
        "clarification_need_details": "Please tell me the item and how much you need.",
        "clarification_min_order": (
            "The basket is below the minimum order amount. What would you like to add?"
        ),
        "option": "{position}, {name}, {pack}, ₹{price}",
        "options_prompt": "Please say first, second, or third, or say no to skip it.",
        "readback_line": "{quantity} packs of {pack} {name} at ₹{price} each",
        "readback_line_single": "{pack} {name} at ₹{price}",
        "readback": (
            "Your basket has {lines}. {fees}The total is ₹{total}. {stores}"
            "Payment is cash on delivery. Delivery address: {address}. Shall I place this order?"
        ),
        "fees": "This includes ₹{fees} in fees. ",
        "stores": "These items come from {stores} stores. ",
        "cart_blocked": "Your basket has {lines}. The total is ₹{total}. {issue}",
        "issue_min_order": "This is below the minimum order amount. What would you like to add?",
        "issue_out_of_stock": "Something in it is out of stock. What would you like to change?",
        "issue_not_serviceable": "It cannot be delivered to this address. Please change the basket.",
        "issue_address_mismatch": (
            "It is set to a different delivery address, so I cannot order it. "
            "Please check the Swiggy app."
        ),
        "issue_payment_unavailable": (
            "Cash on delivery is not available for this basket, so I cannot order it."
        ),
        "issue_other": "Swiggy reports a problem with it, so I cannot order it yet.",
    },
    "hi-IN": {
        "greeting": "नमस्ते। आपको कौन-सा सामान चाहिए?",
        "filler_search": "एक पल, मैं देख रही हूँ।",
        "pack_confirm": "आपकी बताई मात्रा इन पैकों में जोड़ दी है।",
        "confirm_reask": "क्या मैं यह ऑर्डर कर दूँ? कृपया हाँ या नहीं कहें।",
        "ask_what_to_add": "आप क्या जोड़ना चाहेंगे?",
        "what_to_change": "ठीक है, कोई ऑर्डर नहीं हुआ। आप क्या बदलना चाहेंगे?",
        "existing_cart": "आपकी स्विगी टोकरी में पहले से कुछ सामान है। मैं उसे रखूँगी।",
        "item_skipped": "मैंने वह सामान छोड़ दिया है।",
        "order_placed": "आपका डेमो ऑर्डर पूरा हो गया है। यह असली ऑर्डर नहीं था।",
        "order_placed_real": "आपका इंस्टामार्ट ऑर्डर हो गया है और मैंने स्विगी से इसकी जाँच कर ली है।",
        "order_placed_unverified": "आपका डेमो ऑर्डर हो गया, पर मैं इसकी जाँच नहीं कर पाई।",
        "order_placed_unverified_real": (
            "स्विगी के अनुसार आपका ऑर्डर हो गया है, पर मैं अभी इसकी जाँच नहीं कर पाई। "
            "कृपया स्विगी ऐप में देखें। दोबारा ऑर्डर न करें।"
        ),
        "order_partial": "आपके डेमो ऑर्डर का सिर्फ़ एक हिस्सा पूरा हुआ।",
        "order_partial_real": (
            "आपके ऑर्डर का सिर्फ़ एक हिस्सा हुआ है। कृपया स्विगी ऐप में देखें। मैं अपने आप दोबारा कोशिश नहीं करूँगी।"
        ),
        "order_problem": "आपके डेमो ऑर्डर में कोई समस्या है।",
        "order_problem_real": "स्विगी इस ऑर्डर में समस्या दिखा रहा है। कृपया ऐप देखें या कस्टमर केयर को फ़ोन करें।",
        "order_unknown": (
            "मैं पक्का नहीं कर पाई कि आपका ऑर्डर हुआ या नहीं। कृपया दोबारा ऑर्डर न करें। "
            "स्विगी ऐप में देखें या किसी की मदद लें।"
        ),
        "order_rejected": "स्विगी ने ऑर्डर स्वीकार नहीं किया, और कोई ऑर्डर नहीं हुआ।",
        "order_not_attempted": "मैंने ऑर्डर नहीं किया, क्योंकि मैं टोकरी की जाँच नहीं कर पाई।",
        "order_limit": "मैंने ऑर्डर नहीं किया, क्योंकि यह तय सीमा से ज़्यादा है।",
        "checkout_disabled": "अभी ऑर्डर करना बंद है, इसलिए कोई ऑर्डर नहीं हुआ।",
        "order_cancelled": "आपकी अभी की टोकरी रद्द कर दी गई है।",
        "order_already_placed": "यह ऑर्डर पहले ही हो चुका है।",
        "order_in_progress": "कृपया रुकिए, मैं अभी आपके ऑर्डर पर काम कर रही हूँ।",
        "cancel_via_support": "ऑर्डर रद्द करने के लिए कृपया स्विगी कस्टमर केयर को 080-67466729 पर फ़ोन करें।",
        "unknown_user": "मैं आपकी पहचान नहीं कर पाई।",
        "checking_order": "मैं देख रही हूँ कि ऑर्डर हुआ या नहीं।",
        "cart_edit_unclear": "मुझे बदलाव समझ नहीं आया। कृपया फिर से कहें।",
        "cart_changed": "टोकरी में बदलाव हुआ है। पुष्टि से पहले इसे फिर से देख लें।",
        "cart_writes_disabled": "अभी टोकरी बदलना बंद है।",
        "commerce_error": "अभी दुकान से संपर्क नहीं हो पाया। कृपया फिर से कोशिश करें।",
        "not_serviceable": "दुकान अभी इस पते पर डिलीवरी नहीं करती।",
        "item_unavailable": "उनमें से एक सामान अभी उपलब्ध नहीं है।",
        "swiggy_login_expired": "स्विगी लॉगिन खत्म हो गया है, किसी को फिर से साइन इन करना होगा।",
        "intent_error": "मुझे समझ नहीं आया। कृपया फिर से कहें।",
        "clarification_pack_size": "{item} का कौन-सा पैक चाहिए?",
        "clarification_only_bigger_pack": "{item} का सिर्फ बड़ा पैक उपलब्ध है। क्या आप उसे लेना चाहेंगे?",
        "clarification_out_of_stock": "{item} अभी उपलब्ध नहीं है। क्या आप कुछ और लेना चाहेंगे?",
        "clarification_ambiguous_item": "{item} के कई सामान मिले। आपको कौन-सा चाहिए?",
        "clarification_not_found": "{item} नहीं मिला। उसकी जगह क्या जोड़ूँ?",
        "clarification_which_line": "आप टोकरी के किस सामान की बात कर रहे हैं?",
        "clarification_usual_items": "आपके रोज़ के सामान में से क्या जोड़ूँ?",
        "clarification_need_details": "कृपया सामान का नाम और मात्रा बताइए।",
        "clarification_min_order": "टोकरी की रकम कम है। आप और क्या जोड़ना चाहेंगे?",
        "option": "{position}, {name}, {pack}, ₹{price}",
        "options_prompt": "कृपया पहला, दूसरा या तीसरा कहें, या छोड़ने के लिए नहीं कहें।",
        "readback_line": "{pack} {name} के {quantity} पैकेट, ₹{price} प्रति पैकेट",
        "readback_line_single": "{pack} {name}, ₹{price}",
        "readback": (
            "आपकी टोकरी में {lines} हैं। {fees}कुल ₹{total} हैं। {stores}"
            "भुगतान सामान मिलने पर नकद है। पता: {address}। क्या मैं यह ऑर्डर कर दूँ?"
        ),
        "fees": "इसमें ₹{fees} शुल्क शामिल है। ",
        "stores": "यह सामान {stores} दुकानों से आएगा। ",
        "cart_blocked": "आपकी टोकरी में {lines} हैं। कुल ₹{total} हैं। {issue}",
        "issue_min_order": "यह न्यूनतम ऑर्डर रकम से कम है। आप और क्या जोड़ना चाहेंगे?",
        "issue_out_of_stock": "इसमें कुछ सामान उपलब्ध नहीं है। आप क्या बदलना चाहेंगे?",
        "issue_not_serviceable": "यह इस पते पर नहीं पहुँच सकता। कृपया टोकरी बदलें।",
        "issue_address_mismatch": "यह किसी और पते के लिए है, इसलिए मैं ऑर्डर नहीं कर सकती। कृपया स्विगी ऐप देखें।",
        "issue_payment_unavailable": "इस टोकरी पर नकद भुगतान उपलब्ध नहीं है, इसलिए मैं ऑर्डर नहीं कर सकती।",
        "issue_other": "स्विगी इसमें समस्या बता रहा है, इसलिए मैं अभी ऑर्डर नहीं कर सकती।",
    },
    "kn-IN": {
        "greeting": "ನಮಸ್ಕಾರ. ನಿಮಗೆ ಯಾವ ಸಾಮಾನುಗಳು ಬೇಕು?",
        "filler_search": "ಒಂದು ಕ್ಷಣ, ನಾನು ಪರಿಶೀಲಿಸುತ್ತೇನೆ.",
        "pack_confirm": "ನೀವು ಕೇಳಿದ ಪ್ರಮಾಣಕ್ಕೆ ಈ ಪ್ಯಾಕ್‌ಗಳನ್ನು ಸೇರಿಸಿದ್ದೇನೆ.",
        "confirm_reask": "ಈ ಆರ್ಡರ್ ಮಾಡಬೇಕೇ? ದಯವಿಟ್ಟು ಹೌದು ಅಥವಾ ಬೇಡ ಎಂದು ಹೇಳಿ.",
        "ask_what_to_add": "ನೀವು ಇನ್ನೇನು ಸೇರಿಸಬೇಕು?",
        "what_to_change": "ಸರಿ, ಯಾವ ಆರ್ಡರ್ ಆಗಿಲ್ಲ. ಏನು ಬದಲಾಯಿಸಬೇಕು?",
        "existing_cart": "ನಿಮ್ಮ ಸ್ವಿಗ್ಗಿ ಬುಟ್ಟಿಯಲ್ಲಿ ಈಗಾಗಲೇ ಕೆಲವು ಸಾಮಾನುಗಳಿವೆ. ಅವುಗಳನ್ನು ಹಾಗೆಯೇ ಇಡುತ್ತೇನೆ.",
        "item_skipped": "ಆ ಸಾಮಾನನ್ನು ಬಿಟ್ಟಿದ್ದೇನೆ.",
        "order_placed": "ನಿಮ್ಮ ಡೆಮೊ ಆರ್ಡರ್ ಪೂರ್ಣವಾಗಿದೆ. ಇದು ನಿಜವಾದ ಆರ್ಡರ್ ಅಲ್ಲ.",
        "order_placed_real": "ನಿಮ್ಮ ಇನ್‌ಸ್ಟಾಮಾರ್ಟ್ ಆರ್ಡರ್ ಆಗಿದೆ ಮತ್ತು ಸ್ವಿಗ್ಗಿಯಲ್ಲಿ ಪರಿಶೀಲಿಸಿದ್ದೇನೆ.",
        "order_placed_unverified": "ನಿಮ್ಮ ಡೆಮೊ ಆರ್ಡರ್ ಆಗಿದೆ, ಆದರೆ ಪರಿಶೀಲಿಸಲು ಆಗಲಿಲ್ಲ.",
        "order_placed_unverified_real": (
            "ಸ್ವಿಗ್ಗಿ ಪ್ರಕಾರ ನಿಮ್ಮ ಆರ್ಡರ್ ಆಗಿದೆ, ಆದರೆ ನಾನು ಇನ್ನೂ ಪರಿಶೀಲಿಸಲು ಆಗಿಲ್ಲ. "
            "ದಯವಿಟ್ಟು ಸ್ವಿಗ್ಗಿ ಆ್ಯಪ್ ನೋಡಿ. ಮತ್ತೆ ಆರ್ಡರ್ ಮಾಡಬೇಡಿ."
        ),
        "order_partial": "ನಿಮ್ಮ ಡೆಮೊ ಆರ್ಡರ್‌ನ ಒಂದು ಭಾಗ ಮಾತ್ರ ಆಗಿದೆ.",
        "order_partial_real": (
            "ನಿಮ್ಮ ಆರ್ಡರ್‌ನ ಒಂದು ಭಾಗ ಮಾತ್ರ ಆಗಿದೆ. ದಯವಿಟ್ಟು ಸ್ವಿಗ್ಗಿ ಆ್ಯಪ್ ನೋಡಿ. ನಾನಾಗಿ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸುವುದಿಲ್ಲ."
        ),
        "order_problem": "ನಿಮ್ಮ ಡೆಮೊ ಆರ್ಡರ್‌ನಲ್ಲಿ ಸಮಸ್ಯೆ ಇದೆ.",
        "order_problem_real": "ಸ್ವಿಗ್ಗಿ ಈ ಆರ್ಡರ್‌ನಲ್ಲಿ ಸಮಸ್ಯೆ ತೋರಿಸುತ್ತಿದೆ. ದಯವಿಟ್ಟು ಆ್ಯಪ್ ನೋಡಿ ಅಥವಾ ಗ್ರಾಹಕ ಸೇವೆಗೆ ಕರೆ ಮಾಡಿ.",
        "order_unknown": (
            "ನಿಮ್ಮ ಆರ್ಡರ್ ಆಗಿದೆಯೇ ಎಂದು ಖಚಿತವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಆರ್ಡರ್ ಮಾಡಬೇಡಿ. "
            "ಸ್ವಿಗ್ಗಿ ಆ್ಯಪ್ ನೋಡಿ ಅಥವಾ ಯಾರದಾದರೂ ಸಹಾಯ ಪಡೆಯಿರಿ."
        ),
        "order_rejected": "ಸ್ವಿಗ್ಗಿ ಆರ್ಡರ್ ಸ್ವೀಕರಿಸಲಿಲ್ಲ, ಯಾವ ಆರ್ಡರ್ ಆಗಿಲ್ಲ.",
        "order_not_attempted": "ಬುಟ್ಟಿಯನ್ನು ಪರಿಶೀಲಿಸಲು ಆಗದ ಕಾರಣ ಆರ್ಡರ್ ಮಾಡಲಿಲ್ಲ.",
        "order_limit": "ಇದು ಅನುಮತಿಸಿದ ಮಿತಿಗಿಂತ ಹೆಚ್ಚಿರುವುದರಿಂದ ಆರ್ಡರ್ ಮಾಡಲಿಲ್ಲ.",
        "checkout_disabled": "ಈಗ ಆರ್ಡರ್ ಮಾಡುವುದನ್ನು ನಿಲ್ಲಿಸಲಾಗಿದೆ, ಯಾವ ಆರ್ಡರ್ ಆಗಿಲ್ಲ.",
        "order_cancelled": "ಈಗಿನ ಬುಟ್ಟಿಯನ್ನು ರದ್ದು ಮಾಡಲಾಗಿದೆ.",
        "order_already_placed": "ಈ ಆರ್ಡರ್ ಈಗಾಗಲೇ ಆಗಿದೆ.",
        "order_in_progress": "ದಯವಿಟ್ಟು ಕಾಯಿರಿ, ನಿಮ್ಮ ಆರ್ಡರ್ ಪರಿಶೀಲಿಸುತ್ತಿದ್ದೇನೆ.",
        "cancel_via_support": "ಆರ್ಡರ್ ರದ್ದು ಮಾಡಲು ದಯವಿಟ್ಟು ಸ್ವಿಗ್ಗಿ ಗ್ರಾಹಕ ಸೇವೆಗೆ 080-67466729 ಕರೆ ಮಾಡಿ.",
        "unknown_user": "ನಿಮ್ಮನ್ನು ಗುರುತಿಸಲು ಆಗಲಿಲ್ಲ.",
        "checking_order": "ಆರ್ಡರ್ ಆಗಿದೆಯೇ ಎಂದು ಪರಿಶೀಲಿಸುತ್ತಿದ್ದೇನೆ.",
        "cart_edit_unclear": "ನೀವು ಹೇಳಿದ ಬದಲಾವಣೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "cart_changed": "ಬುಟ್ಟಿಯಲ್ಲಿ ಬದಲಾವಣೆ ಆಗಿದೆ. ದೃಢೀಕರಿಸುವ ಮೊದಲು ಮತ್ತೊಮ್ಮೆ ನೋಡಿ.",
        "cart_writes_disabled": "ಈಗ ಬುಟ್ಟಿ ಬದಲಾಯಿಸುವುದನ್ನು ನಿಲ್ಲಿಸಲಾಗಿದೆ.",
        "commerce_error": "ಈಗ ಅಂಗಡಿಯನ್ನು ಸಂಪರ್ಕಿಸಲು ಆಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
        "not_serviceable": "ಅಂಗಡಿ ಈಗ ಈ ವಿಳಾಸಕ್ಕೆ ಡೆಲಿವರಿ ಮಾಡುವುದಿಲ್ಲ.",
        "item_unavailable": "ಅವುಗಳಲ್ಲಿ ಒಂದು ಸಾಮಾನು ಈಗ ಲಭ್ಯವಿಲ್ಲ.",
        "swiggy_login_expired": "ಸ್ವಿಗ್ಗಿ ಲಾಗಿನ್ ಅವಧಿ ಮುಗಿದಿದೆ, ಯಾರಾದರೂ ಮತ್ತೆ ಸೈನ್ ಇನ್ ಮಾಡಬೇಕು.",
        "intent_error": "ನನಗೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "clarification_pack_size": "{item} ಯಾವ ಗಾತ್ರದ ಪ್ಯಾಕ್ ಬೇಕು?",
        "clarification_only_bigger_pack": "{item} ದೊಡ್ಡ ಪ್ಯಾಕ್ ಮಾತ್ರ ಲಭ್ಯವಿದೆ. ಅದನ್ನು ಸೇರಿಸಬೇಕೇ?",
        "clarification_out_of_stock": "{item} ಈಗ ಲಭ್ಯವಿಲ್ಲ. ಬೇರೆ ಏನಾದರೂ ಬೇಕೇ?",
        "clarification_ambiguous_item": "{item} ಒಂದಕ್ಕಿಂತ ಹೆಚ್ಚು ಸಿಕ್ಕಿವೆ. ನಿಮಗೆ ಯಾವುದು ಬೇಕು?",
        "clarification_not_found": "{item} ಸಿಗಲಿಲ್ಲ. ಅದರ ಬದಲು ಏನು ಸೇರಿಸಲಿ?",
        "clarification_which_line": "ಬುಟ್ಟಿಯಲ್ಲಿ ಯಾವ ಸಾಮಾನು ಎಂದು ಹೇಳಿ.",
        "clarification_usual_items": "ನಿಮ್ಮ ಯಾವಾಗಲೂ ತರುವ ಸಾಮಾನುಗಳಲ್ಲಿ ಯಾವುದನ್ನು ಸೇರಿಸಲಿ?",
        "clarification_need_details": "ದಯವಿಟ್ಟು ಸಾಮಾನಿನ ಹೆಸರು ಮತ್ತು ಎಷ್ಟು ಬೇಕು ಎಂದು ಹೇಳಿ.",
        "clarification_min_order": "ಬುಟ್ಟಿಯ ಮೊತ್ತ ಕಡಿಮೆ ಇದೆ. ಇನ್ನೇನು ಸೇರಿಸಬೇಕು?",
        "option": "{position}, {name}, {pack}, ₹{price}",
        "options_prompt": "ದಯವಿಟ್ಟು ಮೊದಲನೆಯದು, ಎರಡನೆಯದು ಅಥವಾ ಮೂರನೆಯದು ಎಂದು ಹೇಳಿ, ಬಿಡಲು ಬೇಡ ಎನ್ನಿ.",
        "readback_line": "{pack} {name} {quantity} ಪ್ಯಾಕೆಟ್, ಒಂದಕ್ಕೆ ₹{price}",
        "readback_line_single": "{pack} {name}, ₹{price}",
        "readback": (
            "ನಿಮ್ಮ ಬುಟ್ಟಿಯಲ್ಲಿ {lines} ಇವೆ. {fees}ಒಟ್ಟು ₹{total}. {stores}"
            "ಪಾವತಿ ಸಾಮಾನು ಬಂದಾಗ ನಗದು. ವಿಳಾಸ: {address}. ಈ ಆರ್ಡರ್ ಮಾಡಲೇ?"
        ),
        "fees": "ಇದರಲ್ಲಿ ₹{fees} ಶುಲ್ಕ ಸೇರಿದೆ. ",
        "stores": "ಈ ಸಾಮಾನುಗಳು {stores} ಅಂಗಡಿಗಳಿಂದ ಬರುತ್ತವೆ. ",
        "cart_blocked": "ನಿಮ್ಮ ಬುಟ್ಟಿಯಲ್ಲಿ {lines} ಇವೆ. ಒಟ್ಟು ₹{total}. {issue}",
        "issue_min_order": "ಇದು ಕನಿಷ್ಠ ಆರ್ಡರ್ ಮೊತ್ತಕ್ಕಿಂತ ಕಡಿಮೆ ಇದೆ. ಇನ್ನೇನು ಸೇರಿಸಬೇಕು?",
        "issue_out_of_stock": "ಇದರಲ್ಲಿ ಕೆಲವು ಸಾಮಾನು ಲಭ್ಯವಿಲ್ಲ. ಏನು ಬದಲಾಯಿಸಬೇಕು?",
        "issue_not_serviceable": "ಇದನ್ನು ಈ ವಿಳಾಸಕ್ಕೆ ತಲುಪಿಸಲು ಆಗುವುದಿಲ್ಲ. ದಯವಿಟ್ಟು ಬುಟ್ಟಿ ಬದಲಾಯಿಸಿ.",
        "issue_address_mismatch": "ಇದು ಬೇರೆ ವಿಳಾಸಕ್ಕೆ ಇದೆ, ಆದ್ದರಿಂದ ಆರ್ಡರ್ ಮಾಡಲು ಆಗುವುದಿಲ್ಲ. ದಯವಿಟ್ಟು ಸ್ವಿಗ್ಗಿ ಆ್ಯಪ್ ನೋಡಿ.",
        "issue_payment_unavailable": "ಈ ಬುಟ್ಟಿಗೆ ನಗದು ಪಾವತಿ ಲಭ್ಯವಿಲ್ಲ, ಆದ್ದರಿಂದ ಆರ್ಡರ್ ಮಾಡಲು ಆಗುವುದಿಲ್ಲ.",
        "issue_other": "ಸ್ವಿಗ್ಗಿ ಇದರಲ್ಲಿ ಸಮಸ್ಯೆ ತೋರಿಸುತ್ತಿದೆ, ಆದ್ದರಿಂದ ಈಗ ಆರ್ಡರ್ ಮಾಡಲು ಆಗುವುದಿಲ್ಲ.",
    },
}

_POSITIONS: dict[str, tuple[str, ...]] = {
    "en-IN": ("First", "Second", "Third"),
    "hi-IN": ("पहला", "दूसरा", "तीसरा"),
    "kn-IN": ("ಮೊದಲನೆಯದು", "ಎರಡನೆಯದು", "ಮೂರನೆಯದು"),
}
_REAL_VARIANTS = frozenset(
    {"order_placed", "order_placed_unverified", "order_partial", "order_problem"}
)


def _spoken_pack(pack: str) -> str:
    # Gnani's text normalization reads "x" as "multiplied by"; a comma reads naturally.
    return re.sub(r"\s*[x×]\s*", ", ", pack)


def _render_lines(say: Say, phrases: dict[str, str]) -> str:
    parts: list[str] = []
    for line in say.lines:
        template = phrases["readback_line_single" if line.quantity == 1 else "readback_line"]
        parts.append(
            template.format(
                quantity=line.quantity,
                pack=_spoken_pack(line.pack_size),
                name=line.name,
                price=format_money(line.unit_price_inr),
            )
        )
    return "; ".join(parts)


def _render_readback(say: Say, phrases: dict[str, str]) -> str:
    fees = say.data.get("fees", "0")
    stores = say.data.get("stores", "1")
    return phrases["readback"].format(
        lines=_render_lines(say, phrases) or say.data.get("lines", ""),
        total=say.data.get("total", ""),
        address=say.data.get("address", say.data.get("address_label", "")),
        fees=phrases["fees"].format(fees=fees) if fees not in {"", "0"} else "",
        stores=phrases["stores"].format(stores=stores) if stores not in {"", "1"} else "",
    )


def _render_clarification(say: Say, phrases: dict[str, str], language: str) -> str:
    kind = say.data.get("kind", "ambiguous_item")
    template = phrases.get(f"clarification_{kind}", phrases["intent_error"])
    parts = [template.format(item=say.data.get("item", ""))]
    positions = _POSITIONS[language]
    for position, option in zip(positions, say.lines, strict=False):
        parts.append(
            phrases["option"].format(
                position=position,
                name=option.name,
                pack=_spoken_pack(option.pack_size),
                price=format_money(option.unit_price_inr),
            )
            + "."
        )
    if say.lines:
        parts.append(phrases["options_prompt"])
    return " ".join(parts)


def response_text(effects: list[Effect], *, language: str, commerce_mode: str) -> str:
    """Render only application-owned language templates, never model error prose.

    `commerce_mode` is required so a real order can never be described as a demo,
    nor a demo as a real order.
    """
    phrases = _PHRASES.get(language)
    if phrases is None:
        raise ValueError("Language must be kn-IN, hi-IN, or en-IN")

    says = [effect for effect in effects if isinstance(effect, Say)]
    # Replies are spoken after the work is finished, so "one moment" only makes sense alone.
    if len(says) > 1:
        says = [say for say in says if say.template != "filler_search"]
    parts: list[str] = []
    for effect in says:
        if effect.template == "readback":
            parts.append(_render_readback(effect, phrases))
        elif effect.template == "cart_blocked":
            issue = phrases.get(
                f"issue_{effect.data.get('issue', 'other')}", phrases["issue_other"]
            )
            parts.append(
                phrases["cart_blocked"].format(
                    lines=_render_lines(effect, phrases) or effect.data.get("lines", ""),
                    total=effect.data.get("total", ""),
                    issue=issue,
                )
            )
        elif effect.template == "clarification":
            parts.append(_render_clarification(effect, phrases, language))
        elif effect.template in _REAL_VARIANTS and commerce_mode != "mock":
            parts.append(phrases[f"{effect.template}_real"])
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
