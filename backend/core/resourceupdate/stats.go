package resourceupdate

import (
	"context"
	"strings"
	"time"

	"gorm.io/gorm"
)

func CountSkillReviewHistoryStats(ctx context.Context, db *gorm.DB, userID string, start, end time.Time, minUserTurns, minToolTurns int) (HistoryStats, error) {
	var stats HistoryStats
	userID = strings.TrimSpace(userID)
	if userID == "" {
		return stats, nil
	}
	err := db.WithContext(ctx).
		Table(
			`(
				SELECT
					ch.conversation_id,
					COUNT(CASE
						WHEN TRIM(COALESCE(ch.raw_content, '')) <> ''
							OR TRIM(COALESCE(ch.content, '')) <> ''
						THEN 1
					END) AS user_turn_count,
					COALESCE(SUM(COALESCE(ch.tool_call_turns, 0)), 0) AS tool_call_count
				FROM chat_histories AS ch
				JOIN conversations AS c ON c.id = ch.conversation_id
				WHERE c.create_user_id = ?
					AND c.deleted_at IS NULL
					AND ch.create_time >= ?
					AND ch.create_time < ?
				GROUP BY ch.conversation_id
			) AS per_conversation`,
			userID,
			start,
			end,
		).
		Select(
			"COALESCE(SUM(user_turn_count), 0) AS user_turn_count, "+
				"COALESCE(SUM(tool_call_count), 0) AS tool_call_count, "+
				"COALESCE(SUM(CASE WHEN user_turn_count >= ? AND tool_call_count >= ? THEN 1 ELSE 0 END), 0) AS qualified_session_count",
			minUserTurns,
			minToolTurns,
		).
		Scan(&stats).Error
	stats.QuantityThreshold = 0
	return stats, err
}
