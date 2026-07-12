/**
 * Module augmentation adding the portal's fields to the Auth.js Session and JWT.
 * These carry the normalized SSO groups, the admin flag, and the access token
 * used to call downstream platform APIs on the user's behalf.
 */
import type { DefaultSession } from "next-auth";
import "next-auth/jwt";

declare module "next-auth" {
  interface Session {
    user: {
      username: string;
      groups: string[];
      isAdmin: boolean;
    } & DefaultSession["user"];
    /** SSO access token, forwarded as a Bearer token to downstream APIs. */
    accessToken?: string;
    /** Set to "RefreshAccessTokenError" when the session needs re-auth. */
    error?: string;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    accessToken?: string;
    refreshToken?: string;
    expiresAt?: number;
    username?: string;
    groups?: string[];
    error?: string;
  }
}
