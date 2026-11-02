package middleware

import (
	model "github.com/cloudreve/Cloudreve/v3/models"
	"github.com/cloudreve/Cloudreve/v3/pkg/hashid"
	"github.com/cloudreve/Cloudreve/v3/pkg/serializer"
	"github.com/gin-gonic/gin"
)

// ValidateSourceLink validates if the perm source link is a valid redirect link
func ValidateSourceLink() gin.HandlerFunc {
	return func(c *gin.Context) {
		var sourceLink *model.SourceLink

		// 先按 HashID 解析：外链和文件都要存在，且 URL 中的文件名要与当前文件名一致
		linkID, ok := c.Get("object_id")
		if !ok {
			if id, err := hashid.DecodeHashID(c.Param("id"), hashid.SourceLinkID); err == nil {
				linkID, ok = id, true
			}
		}
		if ok {
			link, err := model.GetSourceLinkByID(linkID)
			if err == nil && link.File.ID != 0 && link.File.Name == c.Param("name") {
				sourceLink = link
			}
		}

		// 再按自定义标识查找：不检查文件名，文件被替换或改名后外链保持不变
		if sourceLink == nil {
			if link, err := model.GetSourceLinkByCustomKey(c.Param("id")); err == nil {
				sourceLink = link
			}
		}

		if sourceLink == nil {
			c.JSON(200, serializer.Err(serializer.CodeFileNotFound, "", nil))
			c.Abort()
			return
		}

		sourceLink.Downloaded()
		c.Set("source_link", sourceLink)
		c.Next()
	}
}
