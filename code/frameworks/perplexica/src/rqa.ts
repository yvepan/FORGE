/** Apply Appendix F.7 anchoring to native planning/action-selection prompts. */
export function anchorPlanningMessages(messages: any[], rootQuery: string): any[] {
  return messages.map((message: any) => {
    if (message.role !== 'system' || typeof message.content !== 'string') return message;
    const current = message.content;
    const nativePlanner = /^(?:Assistant is an action orchestrator\.|Assistant is a deep-research orchestrator\.)/.test(current.trimStart());
    if (!nativePlanner || !current.includes('You are currently on iteration') || !current.includes('of your research process')) return message;
    return {...message, content: `ROOT USER QUERY (keep the research directed at this objective):\n${rootQuery}\n\nCURRENT SUBTASK OR LEARNINGS:\n${current}\n\nGenerate the next research action.\n\nGenerate follow-up work that materially helps answer the root query. Explore relevant temporal, geographic, mechanistic, or contrastive dimensions, but do not let an individual retrieved source redefine the objective.`};
  });
}
