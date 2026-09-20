import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

export class VoiceAgentContainer extends Container {
  defaultPort = 8080;
  sleepAfter = "10m";
  enableInternet = true;
  envVars = {
    DEEPGRAM_API_KEY: (this.env as any)?.DEEPGRAM_API_KEY || env.DEEPGRAM_API_KEY || "",
  };
}

export default {
  async fetch(
    request: Request,
    workerEnv: { VOICE_AGENT: DurableObjectNamespace },
  ): Promise<Response> {
    return getContainer(workerEnv.VOICE_AGENT).fetch(request);
  },
};
