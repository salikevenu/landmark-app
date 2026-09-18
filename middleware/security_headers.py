import os


def add_security_headers(app):
    """Baseline response hardening.

    Deliberately NOT included: Content-Security-Policy. Every template in
    this app uses inline <style> and inline <script>, so any useful CSP
    would have to allow 'unsafe-inline' -- which buys close to nothing --
    and a strict one would break the entire UI on deploy. Adding CSP
    properly is a separate piece of work (nonces threaded through
    layout_public.html / layout_app.html), not a header flip.
    """

    # HSTS is only meaningful over HTTPS, and sending it from a local
    # http:// dev server would pin localhost to HTTPS in the developer's
    # browser -- a genuinely annoying, hard-to-undo footgun. Gate it on
    # the same signal used for secure cookies.
    _https_only = os.getenv("RENDER") == "true"

    @app.after_request
    def secure_headers(response):

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"

        # Stops the full URL of an authenticated page -- including any
        # ?ref=CODE or ?next=/some/private/path -- from leaking to
        # third-party origins in the Referer header.
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Denies powerful APIs LANDMARK does not use, so an injected or
        # third-party script cannot silently prompt for them.
        #
        # geolocation is (self), NOT () -- the nearby/map features
        # legitimately call navigator.geolocation from this origin, and
        # denying it outright would break them. (self) keeps it working
        # for LANDMARK's own pages while blocking any embedded frame.
        # payment is likewise left unrestricted for the Razorpay checkout.
        response.headers["Permissions-Policy"] = (
            "geolocation=(self), microphone=(), camera=(), usb=()"
        )

        if _https_only:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        return response
