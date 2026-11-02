package model

import (
	"fmt"
	"github.com/cloudreve/Cloudreve/v3/pkg/hashid"
	"github.com/jinzhu/gorm"
	"net/url"
)

// SourceLink represent a shared file source link
type SourceLink struct {
	gorm.Model
	FileID    uint   // corresponding file ID
	Name      string // name of the file while creating the source link, for annotation
	Downloads int    // 下载数
	CustomKey string `gorm:"index:source_link_custom_key"` // 自定义外链标识（/f/ 之后的部分），空值表示使用 HashID

	// 关联模型
	File File `gorm:"save_associations:false:false"`
}

// Key 返回外链中使用的标识：有自定义标识时用自定义标识，否则用 HashID
func (s *SourceLink) Key() string {
	if s.CustomKey != "" {
		return s.CustomKey
	}
	return hashid.HashID(s.ID, hashid.SourceLinkID)
}

// Link gets the URL of a SourceLink
func (s *SourceLink) Link() (string, error) {
	baseURL := GetSiteURL()
	linkPath, err := url.Parse(fmt.Sprintf("/f/%s/%s", s.Key(), s.File.Name))
	if err != nil {
		return "", err
	}
	return baseURL.ResolveReference(linkPath).String(), nil
}

// GetTasksByID queries source link based on ID
func GetSourceLinkByID(id interface{}) (*SourceLink, error) {
	link := &SourceLink{}
	result := DB.Where("id = ?", id).First(link)
	if result.Error != nil {
		return link, result.Error
	}

	files, err := GetFilesByIDs([]uint{link.FileID}, 0)
	if err != nil {
		return link, err
	}
	if len(files) > 0 {
		link.File = files[0]
	}

	return link, nil
}

// GetSourceLinkByCustomKey 根据自定义标识查找外链，外链对应的文件必须仍然存在
func GetSourceLinkByCustomKey(key string) (*SourceLink, error) {
	if !linkKeyPattern.MatchString(key) {
		return nil, ErrLinkKeyInvalid
	}

	link := &SourceLink{}
	if err := DB.Where("custom_key = ?", key).First(link).Error; err != nil {
		return nil, err
	}

	files, err := GetFilesByIDs([]uint{link.FileID}, 0)
	if err != nil {
		return nil, err
	}
	if len(files) == 0 {
		return nil, gorm.ErrRecordNotFound
	}
	link.File = files[0]

	return link, nil
}

// SetCustomKey 设置自定义外链标识；key 为空或等于 HashID 时恢复为 HashID 链接。
// 文件已被删除的外链占着的 key 会转给当前外链。
func (s *SourceLink) SetCustomKey(key string) error {
	if key == hashid.HashID(s.ID, hashid.SourceLinkID) {
		key = ""
	}

	var orphan *SourceLink
	if key != "" {
		var err error
		if orphan, err = s.checkCustomKey(key); err != nil {
			return err
		}
	}

	// gorm 会先把新值写进 s，失败时要恢复
	old := s.CustomKey
	tx := DB.Begin()
	if orphan != nil {
		if err := tx.Model(orphan).UpdateColumn("custom_key", "").Error; err != nil {
			tx.Rollback()
			return err
		}
	}
	if err := tx.Model(s).UpdateColumn("custom_key", key).Error; err != nil {
		tx.Rollback()
		s.CustomKey = old
		return err
	}
	if err := tx.Commit().Error; err != nil {
		s.CustomKey = old
		return err
	}

	s.CustomKey = key
	return nil
}

// checkCustomKey 检查 key 能否作为此外链的自定义标识，
// 返回文件已被删除、需要让出这个 key 的另一个外链（没有则为 nil）
func (s *SourceLink) checkCustomKey(key string) (*SourceLink, error) {
	if !linkKeyPattern.MatchString(key) {
		return nil, ErrLinkKeyInvalid
	}

	// 与某个外链的 HashID 相同时，访问会先按 HashID 解析：
	// 只有那个外链的文件已被删除（或外链本身已删除）才可以用；尚未分配的 ID 将来会被新外链抢走。
	if id, err := hashid.DecodeHashID(key, hashid.SourceLinkID); err == nil && id != s.ID {
		if owner, err := GetSourceLinkByID(id); err == nil {
			if owner.File.ID != 0 {
				return nil, LinkKeyTakenError{Name: owner.File.Name}
			}
		} else if !gorm.IsRecordNotFoundError(err) {
			// 查询出错时不能当作文件已删除，否则会抢走仍在使用的标识
			return nil, err
		} else {
			var last SourceLink
			if DB.Unscoped().Order("id desc").First(&last).Error != nil || id > last.ID {
				return nil, ErrLinkKeyInvalid
			}
		}
	}

	var other SourceLink
	if err := DB.Where("custom_key = ? AND id <> ?", key, s.ID).First(&other).Error; err != nil {
		if gorm.IsRecordNotFoundError(err) {
			return nil, nil
		}
		return nil, err
	}
	files, err := GetFilesByIDs([]uint{other.FileID}, 0)
	if err != nil {
		return nil, err
	}
	if len(files) > 0 {
		return nil, LinkKeyTakenError{Name: files[0].Name}
	}

	return &other, nil
}

// Viewed 增加访问次数
func (s *SourceLink) Downloaded() {
	s.Downloads++
	DB.Model(s).UpdateColumn("downloads", gorm.Expr("downloads + ?", 1))
}
